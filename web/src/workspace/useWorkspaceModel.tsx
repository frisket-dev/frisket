
import { PreviewTable } from '../components/PreviewTable';
import {
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import {
  ApiError,
  MAX_LENS_VIEW_ROWS,
  type CellValue,
  type ColumnDef,
  type GridFilterOperator,
  type GridFilterBboxValue,
  type GridFilterEntityValue,
  type GridFilterSpec,
  type GridSortDirection,
  type HistoryState,
  type LensResolved,
  type ProjectInfo,
  type PreviewSampleResult,
  type PreviewCellDetail,
  type ActionExecutionRequest,
  type RegisteredActionRequest,
  type Row,
  type SavedView,
  type SearchHit,
} from '../api/open';
import { freshActionRequestKey } from '../api/open';
import type { ActionLaunchPrefill } from '../actions/actionFormInitial';
import { canEditProject } from '../api/projectRole';
import { requestGridCellReveal } from '../grid/gridCellReveal';
import { backfillWithConfirmation } from '../api/backfillWithConfirmation';
import { subscribeMapPointsMeta } from '../api/mapPointsMeta';
import type { ColumnGroupViewSpec } from '../grid/SheetGrid';
import { applyColumnTypeRegistry } from '../grid/typeRegistry';
import { computeRowCacheKey, resolveRowCacheScope } from '../grid/rowCacheStore';
import { PanelLoading } from '../components/PanelPrimitives';
import { documentMediaColumns } from '../workbench/documentMedia';
import { selectWorkViewAvailability } from '../core/selectors/workView';
import { WORK_VIEW_KINDS, WORK_VIEW_TITLES } from '../core/selectors/views/registry';
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
import { useWatchRunLinkHandle } from '../bind/useWatchRunLinkHandle';
import { useWorkViewHandle } from '../bind/useWorkViewHandle';
import { useCompareViewHandle } from '../bind/useCompareViewHandle';
import { useLensViewHandle } from '../bind/useLensViewHandle';
import { usePreviewViewHandle } from '../bind/usePreviewViewHandle';
import { useChromeHandle } from '../bind/useChromeHandle';
import { useActSurfaceHandle } from '../bind/useActSurfaceHandle';
import { useActionCatalogHandle } from '../bind/useActionCatalogHandle';
import { usePluginLayoutHandle } from '../bind/usePluginLayoutHandle';
import { useJobsHandle } from '../bind/useJobsHandle';
import {
  applyGridBboxFilterTransition,
  applyGridFilterTransition,
  applyGridSortTransition,
  applySavedViewTransition,
  clearGridFilterTransition,
  clearGridSortTransition,
  setWorkViewResetTransition,
  setWorkViewGridTransition,
  setWorkViewDocumentTransition,
  setWorkViewAnswersTransition,
  setWorkViewGalleryTransition,
  promoteCurrentViewTransition,
  openPromotedTabTransition,
  closePromotedTabTransition,
  actionLaunchForRoute,
  runActionFromSurfaceTransition,
  insertColumnBesideTransition,
  completeAddColumnTransition,
  openCompareTabTransition,
  selectSheetTabTransition,
  toggleProvenanceWithDrawersTransition,
  openRowPanelTransition,
  openColumnPanelTransition,
  openRowRefTransition,
  closeRoutePanelTransition,
  confirmDeleteRowsTransition,
  applyLensTransition,
} from '../state/workspaceTransitions';
import { useProjectDataResource, useRouteHandle } from '../bind/useRouteHandle';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useRouteProjection } from '../bind/useRouteProjection';
import { useRouteRowFetch } from '../bind/useRouteRowFetch';
import { useRouteSyncController } from '../bind/useRouteSyncController';
import { routeStateToRoute, type RouteState } from '../core/route/RouteState';
import { selectActiveSheetId } from '../state/routeStore';
import { previewViewForSheet } from '../state/previewViewStore';
import type { CompareTabSession } from '../state/compareViewStore';
import type { OcrCompareTarget } from '../actions/ocrCompare';
import type { ChromeHandle } from '../core/route/projectRouteState';
import {
  type PromotedView,
  type WorkViewKind,
} from './useWorkspaceChromeState';
import {
  resolveRibbonTabs,
  type ActMenuCommand,
  type RibbonPluginLauncher,
} from '../workbench/actSurface';
import {
  actionsForColumn,
} from '../actions/model';

import {
  navigate,
  replaceRoute,
  routePath,
  type RoutePanel,
} from '../routes';
import {
  GridWorkbenchViewFrame,
} from '../workbench/contributions';
import {
  dataRequirementReason,
  firstMissingDataRequirement,
  sheetDataRequirementContext,
  type WorkbenchDataRequirementContext,
} from '../workbench/dataRequirements';
import {
  FIRST_PARTY_WORKBENCH_CONTRIBUTION_DESCRIPTORS,
  IMAGE_GALLERY_VIEW_DESCRIPTOR,
  isProjectionViewDescriptor,
  OCR_COMPARE_COMMAND_DESCRIPTOR,
  TRANSCRIBE_COMPARE_COMMAND_DESCRIPTOR,
  TRANSLATE_COMPARE_COMMAND_DESCRIPTOR,
  TOPIC_COMPARE_COMMAND_DESCRIPTOR,
  OPEN_SOURCES_COMMAND_DESCRIPTOR,
  OPEN_SETTINGS_COMMAND_DESCRIPTOR,
  normalizePlacement,
  type WorkbenchHostId,
  type WorkbenchViewDescriptor,
} from '../workbench/descriptors';
import {
  projectHostIdentity,
  type WorkbenchHostContext,
} from '../workbench/hostContext';
import {
  resolveWorkbenchLayout,
  type WorkbenchResolvedLayoutContribution,
  type WorkbenchStampedContributionDescriptor,
} from '../workbench/layout';
import { PluginDetailHost } from '../workbench/PluginDetailHost';
import type { PluginDetailSubject } from '../workbench/pluginDetailContext';
import { resolvePluginProjectionViewAvailability } from '../workbench/pluginProjectionViewContext';
import { resolvePluginViewAvailability } from '../workbench/pluginViewContext';
import {
  pluginCommandDescriptorsFromRuntimeIndex,
  pluginPanelDescriptorsFromRuntimeIndex,
  pluginViewDescriptorsFromRuntimeIndex,
} from '../workbench/pluginRuntimeDescriptors';
import {
  firstPartyCommandEntry,
  pluginCommandEntry,
  type WorkbenchCommandEntry,
} from '../workbench/commandRegistry';
import {
  type PaletteActionItem,
  type PaletteGotoItem,
} from '../workbench/WorkbenchCommandPalette';
import {
  useWorkbenchContributionVisibility,
} from './useWorkbenchContributionVisibility';
import {
  isSupportedWorkbenchVisibilityTarget,
  workbenchVisibilityTargetFromContribution,
  type WorkbenchVisibilityTarget,
} from '../workbench/visibility';
import { usePluginPeekController } from './usePluginPeekController';
import { useRunController } from '../bind/useRunController';
import {
  filterOperatorForColumnType,
  type ChildFilter,
  type HeaderMenuState,
} from './workspaceState';
import { useWorkspaceChromeState } from './useWorkspaceChromeState';
import {
  LENS_REFRESH_NEEDED_CODES,
  normalizeGridFilterSpec,
  normalizeGridSortSpec,
  clampFrozenColumnCount,
  columnOrderKey,
  exactViewHiddenColumns,
  filterConditionFromDraft,
  frozenColumnsKey,
  hasCustomColumnOrder,
  hiddenColumnsKey,
  shownDefaultColumnsKey,
  loadColumnOrder,
  loadFrozenColumnCount,
  loadHiddenColumns,
  normalizeColumnOrder,
  spliceColumnOrderBeside,
} from './gridColumnState';
import { AddColumnPopover } from './popovers';
import { ReplayAcceptPopover } from './ReplayAcceptPopover';
import type { ColumnAnnotation } from '../grid/columnAnnotations';
import {
  advanceAfterAction,
  buildReplayPendingAnnotation,
  firstPendingRowId,
  replayPendingChipLabel,
} from '../grid/replayPending';
import {
  EMPTY_MISSING_CONTRIBUTION_IDS,
  EVIDENCE_CONTRIBUTION_ID,
  GRAPH_NEIGHBORHOOD_CONTRIBUTION_ID,
  SAVED_VIEWS_CONTRIBUTION_ID,
  SOURCE_HEALTH_CONTRIBUTION_ID,
  WATCHES_CONTRIBUTION_ID,
} from './contributionIds';
import { LazySheetGrid } from './lazyGridView';

const EMPTY_ROW_CACHE_SNAPSHOT = { key: '', version: 0, totalRows: null };
const subscribeNoop = () => () => undefined;
const getEmptyRowCacheSnapshot = () => EMPTY_ROW_CACHE_SNAPSHOT;

function groupManagedHiddenColumnNames(groups: ColumnGroupViewSpec[]): Set<string> {
  const hidden = new Set<string>();
  for (const group of groups) {
    if (!group.show_justification) {
      for (const name of group.columns) {
        if (name === 'justification' || name.endsWith('_justification')) hidden.add(name);
      }
    }
    if (!group.show_confidence) {
      for (const name of group.columns) {
        if (name === 'confidence' || name.endsWith('_confidence')) hidden.add(name);
      }
    }
  }
  return hidden;
}

function stampRuntimeIndex<T extends WorkbenchStampedContributionDescriptor['descriptor']>(
  descriptors: T[],
): WorkbenchStampedContributionDescriptor[] {
  return descriptors.map((descriptor) => ({ descriptor, runtimeSource: 'runtimeIndex' }));
}

interface ReplayReviewEntry {
  rowId: string;
  edit: CellValue;
  fresh: CellValue;
  hash: string;
  runId: string;
}
const REPLAY_REVIEW_PAGE_SIZE = 500;

const NO_CITED_COLUMNS: readonly string[] = [];
const NO_CITED_COLUMNS_RESOLVED: readonly ColumnDef[] = [];

export function useWorkspaceModel({
  project,
  routeSheetId,
  routeActionKind,
  routeReview,
  routePanel,
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
  const afterBackfill = job.afterBackfill;
  const requestCostConfirmation = job.requestCostConfirmation;

  const gridView = useGridViewHandle();
  const rowCache = useRowCacheHandle();
  const selection = useSelectionHandle();
  const detail = useDetailHandle();
  const savedViews = useSavedViewsHandle();
  const watchRunLink = useWatchRunLinkHandle();
  const workView = useWorkViewHandle();
  const compareView = useCompareViewHandle();
  const lensViewHandle = useLensViewHandle();
  const previewViewHandle = usePreviewViewHandle();
  const chrome = useChromeHandle();
  // This is the sole browser-history writer.
  const route = useRouteHandle();
  const activeSheetId = useSelector(route.store, selectActiveSheetId);
  const writeRoute = useCallback((next: RouteState) => route.navigate(next), [route]);
  const draftSortColumn = useSelector(gridView.store, (s) => s.draft.sortColumn);
  const draftSortDirection = useSelector(gridView.store, (s) => s.draft.sortDirection);
  const activeGridFilter = useSelector(gridView.store, (s) => s.applied.filter);
  const activeGridSort = useSelector(gridView.store, (s) => s.applied.sort);
  const rowHeight = useSelector(gridView.store, (s) => s.rowHeight);
  const wrapText = useSelector(gridView.store, (s) => s.wrapText);
  const columnGroupSpecs = useSelector(gridView.store, (s) => s.columnGroupSpecs);
  const columnOrderBySheet = useSelector(gridView.store, (s) => s.columnOrderBySheet);
  const frozenColumnCountBySheet = useSelector(gridView.store, (s) => s.frozenColumnCountBySheet);
  const hiddenColumnsBySheet = useSelector(gridView.store, (s) => s.hiddenColumnsBySheet);
  const columnGroupsVersion = useSelector(gridView.store, (s) => s.columnGroupsVersion);

  useEffect(() => {
    projectApi
      .listColumnTypes()
      .then(applyColumnTypeRegistry)
      .catch(() => undefined);
  }, [project.id]);

  const provenanceOpen = useSelector(chrome.store, (s) => s.provenanceOpen);
  const overflowMenuOpen = useSelector(chrome.store, (s) => s.overflowMenuOpen);

  const selectedColumnId = useSelector(selection.store, (s) => s.selectedColumnId);
  const selectedRows = useSelector(selection.store, (s) => s.selectedRows);

  const rowDrawer = useSelector(detail.store, (s) => s.rowDrawer);
  const columnDrawer = useSelector(detail.store, (s) => s.columnDrawer);
  const childFilter = useSelector(detail.store, (s) => s.childFilter);
  const headerMenu = useSelector(detail.store, (s) => s.headerMenu);

  const views = useSelector(savedViews.store, (s) => s.views);
  const viewName = useSelector(savedViews.store, (s) => s.viewName);
  const savedViewsEditor = useSelector(savedViews.store, (s) => s.editor);
  const savedViewsSaving = useSelector(savedViews.store, (s) => s.isSaving);
  const savedViewsConfirmation = useSelector(savedViews.store, (s) => s.confirmation);
  const savedViewsConfirmationError = useSelector(savedViews.store, (s) => s.confirmationError);
  const savedViewsLastPublication = useSelector(savedViews.store, (s) => s.lastPublication);
  const activeSavedViewId = useSelector(gridView.store, (s) => s.activeSavedViewId);

  useEffect(() => {
    if (activeSavedViewId !== null && !views.some((view) => view.id === activeSavedViewId)) {
      gridView.clearActiveSavedView(activeSavedViewId);
    }
  }, [activeSavedViewId, gridView, views]);

  const lensView = useSelector(lensViewHandle.store, (s) => s.lensView);
  const lensOpenError = useSelector(lensViewHandle.store, (s) => s.lensOpenError);
  const previewView = useSelector(previewViewHandle.store, (s) => s.previewView);
  const gridOnlySheetId = useSelector(workView.store, (s) => s.gridOnlySheetId);
  const activePromotedKey = useSelector(workView.store, (s) => s.activePromotedKey);
  const ocrCompareOpen = useSelector(compareView.store, (s) => s.ocr.open);
  const ocrCompareActive = useSelector(compareView.store, (s) => s.ocr.active);
  const ocrCompareSession = useSelector(compareView.store, (s) => s.ocr.session);
  const ocrCompareCloseWarn = useSelector(compareView.store, (s) => s.ocr.closeWarn);
  const [ocrCompareTarget, setOcrCompareTarget] = useState<OcrCompareTarget | null>(null);
  const transcribeCompareOpen = useSelector(compareView.store, (s) => s.transcribe.open);
  const transcribeCompareActive = useSelector(compareView.store, (s) => s.transcribe.active);
  const transcribeCompareSession = useSelector(compareView.store, (s) => s.transcribe.session);
  const transcribeCompareCloseWarn = useSelector(compareView.store, (s) => s.transcribe.closeWarn);
  const translateCompareOpen = useSelector(compareView.store, (s) => s.translate.open);
  const translateCompareActive = useSelector(compareView.store, (s) => s.translate.active);
  const translateCompareSession = useSelector(compareView.store, (s) => s.translate.session);
  const translateCompareCloseWarn = useSelector(compareView.store, (s) => s.translate.closeWarn);
  const topicCompareOpen = useSelector(compareView.store, (s) => s.topic.open);
  const topicCompareActive = useSelector(compareView.store, (s) => s.topic.active);
  const topicCompareSession = useSelector(compareView.store, (s) => s.topic.session);
  const topicCompareCloseWarn = useSelector(compareView.store, (s) => s.topic.closeWarn);
  const answersViewSheetId = useSelector(workView.store, (s) => s.answersViewSheetId);

  const {
    actionPanelOpen,
    clearDeleteRowsConfirm,
    closeCopilotPopover,
    copilotPopoverOpen,
    closeEvidenceViewer,
    commandPaletteOpen,
    deleteRowsConfirm,
    error,
    evidenceViewerState,
    lastCommandAction,
    hideActionPanel,
    openActionPanel,
    openCommandPalette,
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
    promotedViews,
    setPromotedViews,
    openSplit,
    setOpenSplit,
    documentView,
    setDocumentView,
    documentAnnotationPreferences,
    setSheetAnnotationToggles,
  } = useWorkspaceChromeState(project.id);
  const {
    hiddenContributionIds,
    hiddenContributionIdSet,
    hideContribution,
    isContributionHidden,
    revealContribution,
  } = useWorkbenchContributionVisibility(project.id);
  const pluginLayout = usePluginLayoutHandle();
  const workbenchPluginRuntimeIndex = useSelector(
    pluginLayout.store,
    (s) => s.workbenchPluginRuntimeIndex,
  );
  const reviewOpen = routeReview === true;
  // A run-scoped queue reuses the same review route and overlay. The scope is
  // deliberately transient: it is an inspection affordance, not a saved view.
  const [reviewRunId, setReviewRunId] = useState<string | null>(null);

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

  const changeRowHeight = useCallback(
    (n: number) => {
      gridView.setRowHeight(n);
      localStorage.setItem('frisket:row-height', String(n));
    },
    [gridView],
  );
  const toggleWrapText = useCallback(() => {
    const before = gridView.store.get();
    gridView.toggleWrap();
    const after = gridView.store.get();
    localStorage.setItem('frisket:wrap-text', after.wrapText ? '1' : '0');
    if (after.rowHeight !== before.rowHeight) {
      localStorage.setItem('frisket:row-height', String(after.rowHeight));
    }
  }, [gridView]);

  const refreshSheets = useCallback(
    () =>
      projectData
        .refresh('sheets')
        .then(() => {
          const normalized = projectData.normalize(route.store.get());
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
    [pluginLayout],
  );
  useEffect(() => {
    const initial = projectData.start();
    void initial.sheets
      .then(() => {
        const normalized = projectData.normalize(route.store.get());
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

  useEffect(() => {
    const onOpenEvidence = (event: Event) => {
      const detail = (event as CustomEvent<{ evidenceLinkId?: unknown }>).detail;
      const evidenceLinkId = detail?.evidenceLinkId;
      if (typeof evidenceLinkId === 'string' || typeof evidenceLinkId === 'number') {
        openEvidenceViewer(evidenceLinkId);
      }
    };
    window.addEventListener('frisket:open-evidence', onOpenEvidence);
    return () => window.removeEventListener('frisket:open-evidence', onOpenEvidence);
  }, [openEvidenceViewer]);

  const selectSheet = useCallback(
    (id: string) => {
      writeRoute({
        projectId: project.id,
        sheetId: id,
        actionKind: null,
        review: false,
        panel: null,
      });
    },
    [project.id, writeRoute],
  );

  const sheet = sheets.find((candidate) => candidate.id === activeSheetId);
  const closeActionDrawerRef = useRef<() => void>(() => {});
  const onLaunchAccepted = useCallback(() => closeActionDrawerRef.current(), []);
  const { startProposal, startRun } = useRunController({
    invalidateProjectData: projectData.invalidate,
    refreshHistory,
    refreshReviewCount,
    refreshSheets,
    sheet,
    showError,
    onLaunchAccepted,
    onMaterializedSheetCreated: selectSheet,
  });
  const onMainViewTabKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLButtonElement>, sheetId: string) => {
      const currentIndex = sheets.findIndex((candidate) => candidate.id === sheetId);
      if (currentIndex < 0 || sheets.length === 0) return;
      const lastIndex = sheets.length - 1;
      const nextIndex =
        event.key === 'ArrowRight'
          ? (currentIndex + 1) % sheets.length
          : event.key === 'ArrowLeft'
            ? (currentIndex + lastIndex) % sheets.length
            : event.key === 'Home'
              ? 0
              : event.key === 'End'
                ? lastIndex
                : -1;
      if (nextIndex < 0) return;
      event.preventDefault();
      const nextSheetId = sheets[nextIndex].id;
      selectSheet(nextSheetId);
      requestAnimationFrame(() => {
        document.querySelector<HTMLElement>(
          `[data-testid="workbench-mainView-tab-${nextSheetId}"]`,
        )?.focus();
      });
    },
    [selectSheet, sheets],
  );
  const selectBottomDockTab = useCallback(
    (contribution: WorkbenchResolvedLayoutContribution) => {
      setActiveBottomDockTab(contribution.placementId);
      if (contribution.contributionId === 'frisket.core.panel.errors') {
        void pluginLayout.refresh();
      }
    },
    [pluginLayout, setActiveBottomDockTab],
  );
  const activeLensView =
    lensView && sheet?.id === lensView.sheetId ? lensView : null;
  const activePreviewView = previewViewForSheet(previewView, sheet?.id);
  const teardownPreviewJob = useCallback(
    () => previewViewHandle.teardownPreviewJob(),
    [previewViewHandle],
  );
  useEffect(() => () => teardownPreviewJob(), [teardownPreviewJob]);
  useEffect(
    () => route.registerSheetChangeTeardown(teardownPreviewJob),
    [route, teardownPreviewJob],
  );
  const selectedRowIdsForSheet = useMemo(
    () => (sheet && selectedRows.sheetId === sheet.id ? selectedRows.rowIds : []),
    [sheet, selectedRows.sheetId, selectedRows.rowIds],
  );
  const effectiveSortColumn = sheet?.columns.some((column) => column.name === draftSortColumn)
    ? draftSortColumn
    : sheet?.columns[0]?.name ?? '';
  const activeColumnOrder = useMemo(
    () => (sheet
      ? normalizeColumnOrder(
          columnOrderBySheet[sheet.id] ?? loadColumnOrder(project.id, sheet),
          sheet,
        )
      : []),
    [columnOrderBySheet, project.id, sheet],
  );
  const columnOrderIsCustom = sheet ? hasCustomColumnOrder(activeColumnOrder, sheet) : false;
  const storedActiveHiddenColumns = useMemo(
    () => (sheet ? hiddenColumnsBySheet[sheet.id] ?? loadHiddenColumns(project.id, sheet) : []),
    [hiddenColumnsBySheet, project.id, sheet],
  );
  const groupManagedHiddenColumns = useMemo(
    () => groupManagedHiddenColumnNames(columnGroupSpecs),
    [columnGroupSpecs],
  );
  const activeSavedView = useMemo(
    () => activeSavedViewId === null || !sheet
      ? null
      : views.find(
          (view) => view.id === activeSavedViewId && String(view.sheet_id) === sheet.id,
        ) ?? null,
    [activeSavedViewId, sheet, views],
  );
  const activeExactViewColumns = useMemo(() => {
    const raw = activeSavedView?.spec.columns;
    return Array.isArray(raw)
      ? raw.filter((name): name is string => typeof name === 'string')
      : null;
  }, [activeSavedView]);
  const activeExactHiddenColumns = useMemo(() => {
    if (!sheet || activeExactViewColumns === null) return [];
    return exactViewHiddenColumns(sheet, activeExactViewColumns, groupManagedHiddenColumns);
  }, [activeExactViewColumns, groupManagedHiddenColumns, sheet]);
  const activeHiddenColumns = useMemo(
    () => [...new Set([...storedActiveHiddenColumns, ...activeExactHiddenColumns])],
    [activeExactHiddenColumns, storedActiveHiddenColumns],
  );

  useEffect(() => {
    if (!sheet || !activeSavedView || activeExactViewColumns === null) return;
    if (JSON.stringify(storedActiveHiddenColumns) === JSON.stringify(activeExactHiddenColumns)) return;
    if (gridView.store.get().activeSavedViewId !== activeSavedView.id) return;
    gridView.reconcileSavedViewHiddenColumns(
      activeSavedView.id,
      sheet.id,
      activeExactHiddenColumns,
    );
    if (gridView.store.get().activeSavedViewId === activeSavedView.id) {
      localStorage.setItem(
        hiddenColumnsKey(project.id, sheet.id),
        JSON.stringify(activeExactHiddenColumns),
      );
    }
  }, [
    activeExactHiddenColumns,
    activeExactViewColumns,
    activeSavedView,
    gridView,
    project.id,
    sheet,
    storedActiveHiddenColumns,
  ]);

  const activeDisplayHiddenColumns = useMemo(() => {
    const hidden = new Set(activeHiddenColumns);
    for (const name of groupManagedHiddenColumns) hidden.add(name);
    return [...hidden];
  }, [activeHiddenColumns, groupManagedHiddenColumns]);
  const currentViewColumns = useMemo(() => {
    if (!sheet) return null;
    const defaultOrder = sheet.columns.map((column) => column.name);
    const visible = activeColumnOrder.filter((name) => !activeDisplayHiddenColumns.includes(name));
    return columnOrderIsCustom || activeDisplayHiddenColumns.length > 0 ||
      JSON.stringify(visible) !== JSON.stringify(defaultOrder)
      ? visible
      : null;
  }, [activeColumnOrder, activeDisplayHiddenColumns, columnOrderIsCustom, sheet]);
  const headerMenuColumnStillVisible =
    !!headerMenu && !!sheet?.columns.some((column) => column.id === headerMenu.column.id);
  const visibleHeaderMenu = headerMenuColumnStillVisible ? headerMenu : null;
  const activeChildFilter =
    childFilter && sheet && childFilter.sheetId === sheet.id ? childFilter : null;
  const workspaceRoute = useCallback(
    () => ({
      kind: 'project' as const,
      projectId: project.id,
      sheetId: activeSheetId ?? undefined,
    }),
    [activeSheetId, project.id],
  );
  const routeStateBase = useCallback(
    (): RouteState => ({
      projectId: project.id,
      sheetId: activeSheetId,
      actionKind: null,
      review: false,
      panel: null,
    }),
    [activeSheetId, project.id],
  );

  // Routes carry exact catalog action IDs; never start work here.
  const runActionFromSurface = useCallback(
    (
      rawActionKind: string,
      sourceColumn?: string,
      initial?: ActionLaunchPrefill,
    ) => {
      const actionKind = rawActionKind;
      runActionFromSurfaceTransition(
        { actSurface, chrome, route },
        actionKind,
        sourceColumn,
        { ...routeStateBase(), actionKind },
        initial,
      );
    },
    [actSurface, chrome, route, routeStateBase],
  );

  const copilotOpen = copilotPopoverOpen;

  const commandPaletteQuery = useSelector(chrome.store, (s) => s.commandPaletteQuery);
  const setCommandPaletteQuery = chrome.setCommandPaletteQuery;
  const closeCommandPaletteAndReset = chrome.closeCommandPaletteAndReset;

  const openImportDialog = actSurface.openImportDialog;

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const meta = event.metaKey || event.ctrlKey;
      if (!meta) return;
      const key = event.key.toLowerCase();
      if (key === 'k' && !event.shiftKey && !event.altKey) {
        event.preventDefault();
        if (commandPaletteOpen) {
          closeCommandPaletteAndReset();
        } else {
          openCommandPalette();
        }
      } else if (key === 'p' && event.shiftKey) {
        event.preventDefault();
        openCommandPalette();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [commandPaletteOpen, openCommandPalette, closeCommandPaletteAndReset]);

  const openSourcesConnections = useCallback(() => {
    actSurface.openSourcesConnections();
    window.dispatchEvent(new Event('frisket:sources-changed'));
  }, [actSurface]);

  const openSourcesFromCommandPalette = useCallback(() => {
    openSourcesConnections();
    recordCommandAction('Opened Sources');
    closeCommandPaletteAndReset();
  }, [closeCommandPaletteAndReset, openSourcesConnections, recordCommandAction]);

  // Detached chrome requests the same focused manager through a window event.
  useEffect(() => {
    const onOpenSources = () => openSourcesConnections();
    window.addEventListener('frisket:open-sources', onOpenSources);
    return () => window.removeEventListener('frisket:open-sources', onOpenSources);
  }, [openSourcesConnections]);

  const hideWorkbenchContribution = useCallback(
    (target: WorkbenchVisibilityTarget) => {
      if (!isSupportedWorkbenchVisibilityTarget(target)) {
        return;
      }
      hideContribution(target.contributionId);
      recordCommandAction(`Hid ${target.title}`);
    },
    [hideContribution, recordCommandAction],
  );

  const revealWorkbenchContribution = useCallback(
    (target: WorkbenchVisibilityTarget) => {
      if (!isSupportedWorkbenchVisibilityTarget(target)) {
        return;
      }
      revealContribution(target.contributionId);
      recordCommandAction(`Showed ${target.title}`);
    },
    [recordCommandAction, revealContribution],
  );

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

  const openSettingsFromCommandPalette = useCallback(() => {

    navigate({ kind: 'settings', projectId: project.id, scope: 'project', section: 'general' });
    recordCommandAction('Opened Settings');
  }, [project.id, recordCommandAction]);

  const openNotificationSettings = useCallback(() => {
    navigate({ kind: 'settings', projectId: project.id, scope: 'project', section: 'notifications' });
  }, [project.id]);

  const commandPaletteActionItems = useMemo<PaletteActionItem[]>(
    () => actActionTemplates.map((template) => ({
      actionKind: template.kind,
      name: template.name,
      keywords: template.keywords,
    })),
    [actActionTemplates],
  );

  const commandPaletteBestMatchItems = useMemo<PaletteActionItem[]>(() => {
    if (!sheet) return [];

    const MEDIA_TYPES = new Set(['file', 'media', 'image', 'video', 'audio', 'link']);
    const rankedColumns = sheet.columns.toSorted((a, b) => {
      const aMedia = MEDIA_TYPES.has(String(a.type)) ? 0 : 1;
      const bMedia = MEDIA_TYPES.has(String(b.type)) ? 0 : 1;
      return aMedia - bMedia;
    });
    const seen = new Set<string>();
    const items: PaletteActionItem[] = [];
    for (const column of rankedColumns) {
      for (const template of actionsForColumn(actActionTemplates, column)) {
        if (seen.has(template.kind)) continue;
        seen.add(template.kind);
        items.push({
          actionKind: template.kind,
          name: template.name,
          keywords: template.keywords,
          sourceColumn: column.name,
          columnType: String(column.type),
        });
        if (items.length >= 5) break;
      }
      if (items.length >= 5) break;
    }
    return items;
  }, [actActionTemplates, sheet]);

  const commandPaletteGotoItems = useMemo<PaletteGotoItem[]>(
    () => sheets.map((s) => ({ sheetId: String(s.id), name: s.name })),
    [sheets],
  );

  const navigateToSearchHit = useCallback(
    (hit: SearchHit) => {
      const sheetId = String(hit.sheet_id);
      const rowId = String(hit.row_id);
      requestGridCellReveal({
        projectId: project.id,
        sheetId,
        rowId,
        columnId: String(hit.column_id),
        columnName: hit.column_name,
      });
      selectSheet(sheetId);
      window.dispatchEvent(
        new CustomEvent('frisket:reveal-row', { detail: { sheetId, rowId } }),
      );
    },
    [project.id, selectSheet],
  );

  const closeHeaderMenu = useCallback(() => {
    detail.closeHeaderMenu();
  }, [detail]);

  const routeChrome = useMemo<ChromeHandle>(
    () => ({
      hydrateOpenSplit(spec) {
        if (!sheet) return;
        if (spec.kind === 'map') {
          const col = sheet.columns.find((column) => column.id === spec.columnId);
          if (col?.type === 'geo_point') {
            setOpenSplit({ kind: 'map', sheetId: sheet.id, columnId: col.id });
          }
        } else {
          setOpenSplit({ kind: 'graph', sheetId: sheet.id });
        }
      },
    }),
    [sheet, setOpenSplit],
  );
  const commitRoute = useCallback((next: RouteState, mode: 'push' | 'replace') => {
    const nextRoute = routeStateToRoute(next);
    if (mode === 'replace') replaceRoute(nextRoute);
    else navigate(nextRoute);
  }, []);
  useRouteSyncController(route, commitRoute);
  useRouteProjection(
    {
      projectId: project.id,
      routeSheetId: routeSheetId ?? null,
      routeActionKind: routeActionKind ?? null,
      routeReview: !!routeReview,
      routePanel,
    },
    route,
    routeChrome,
    !!sheet,
  );
  useRouteRowFetch({
    route,
    detail,
    selection,
    sheet,
    activeChildFilterParentRowId: activeChildFilter?.parentRowId ?? null,
    activeGridFilter,
    activeGridSort,
    rowDrawerId: rowDrawer?.id ?? null,
    showError,
  });

  const openRowPanel = useCallback(
    (row: Row, col?: ColumnDef | null, preview?: PreviewCellDetail | null) => {

      openRowPanelTransition({ detail, selection }, writeRoute, routeStateBase(), row, col, preview);
    },
    [detail, routeStateBase, selection, writeRoute],
  );

  const openColumnPanel = useCallback(
    (col: ColumnDef) => {

      openColumnPanelTransition({ detail, selection }, writeRoute, routeStateBase(), col);
    },
    [detail, routeStateBase, selection, writeRoute],
  );

  const walkDetailRow = useCallback(
    (direction: 1 | -1) => {
      if (!sheet || !rowDrawer) return;
      const targetIndex = rowDrawer.index + direction;
      if (targetIndex < 0) return;
      const scope = {
        parentRowId: activeChildFilter?.parentRowId ?? null,
        filter: activeGridFilter,
        sort: activeGridSort,
      };
      void projectApi
        .getSheetData(sheet.id, targetIndex, 1, scope)
        .then((page) => {
          const nextRow = page.rows[0];
          if (!nextRow) return;
          openRowPanel(nextRow);
          selection.setSelectedRows({
            sheetId: sheet.id,
            rowIds: [nextRow.id],
            rowIndexes: [nextRow.index],
          });
        })
        .catch((e: Error) => showError(e.message));
    },
    [
      activeChildFilter?.parentRowId,
      activeGridFilter,
      activeGridSort,
      openRowPanel,
      rowDrawer,
      selection,
      sheet,
      showError,
    ],
  );

  const openMapPanel = useCallback(
    (col: ColumnDef) => {
      if (col.type !== 'geo_point') {
        return;
      }
      if (!mapContributionId || isContributionHidden(mapContributionId)) {
        return;
      }
      if (!sheet) return;
      setOpenSplit({ kind: 'map', sheetId: sheet.id, columnId: col.id });
    },
    [isContributionHidden, mapContributionId, setOpenSplit, sheet],
  );

  const openGraphPanel = useCallback(() => {
    if (isContributionHidden(GRAPH_NEIGHBORHOOD_CONTRIBUTION_ID)) {
      return;
    }
    if (!sheet) return;
    setOpenSplit({ kind: 'graph', sheetId: sheet.id });
  }, [isContributionHidden, setOpenSplit, sheet]);

  const openMapSplit = useCallback(() => {
    const geoColumn = sheet?.columns.find((column) => column.type === 'geo_point');
    if (geoColumn) {
      openMapPanel(geoColumn);
    }
  }, [openMapPanel, sheet]);

  const openSourceHealthMainView = useCallback(
    (sourceId: number) => {
      if (isContributionHidden(SOURCE_HEALTH_CONTRIBUTION_ID)) {
        return;
      }
      writeRoute({
        ...routeStateBase(),
        panel: { kind: 'sourceHealth', sourceId: String(sourceId) },
      });
    },
    [isContributionHidden, routeStateBase, writeRoute],
  );

  const openEvidenceViewerForWorkspace = useCallback((
    linkId: string | number,
    host: 'modalOrPeek' | 'mainView' = 'modalOrPeek',
  ) => {
    if (host === 'mainView' && isContributionHidden(EVIDENCE_CONTRIBUTION_ID)) {
      return;
    }
    openEvidenceViewer(linkId, host);
  }, [isContributionHidden, openEvidenceViewer]);

  const openRowById = useCallback(
    (rowId: string) => {
      writeRoute({ ...routeStateBase(), panel: { kind: 'row', rowId } });
      if (sheet) {
        selection.setSelectedRows({ sheetId: sheet.id, rowIds: [rowId], rowIndexes: [] });
      }
    },
    [routeStateBase, writeRoute, selection, sheet],
  );

  // Keep an already-open Detail reader aligned with Document view without
  // converting document focus into an action row selection.
  const selectDocumentRow = useCallback(
    (rowId: string) => {
      if (!sheet) return;
      if (routePanel?.kind === 'row' && routePanel.rowId !== rowId) {
        writeRoute({ ...routeStateBase(), panel: { kind: 'row', rowId } });
      }
    },
    [sheet, routePanel, routeStateBase, writeRoute],
  );

  // Answers is a selection-synced row browser; Document focus is deliberately
  // local reading state. Separate callbacks keep a Document selection fix from
  // making Answers row clicks snap back to the first row.
  const selectAnswersRow = useCallback(
    (rowId: string) => {
      if (!sheet) return;
      selection.setSelectedRows({ sheetId: sheet.id, rowIds: [rowId], rowIndexes: [] });
    },
    [selection, sheet],
  );

  const openRowInDocumentView = useCallback(
    (rowId: string, columnId: string) => {
      if (!sheet) return;
      setOpenSplit(null);
      const base =
        documentView && documentView.sheetId === sheet.id
          ? documentView
          : {
              sheetId: sheet.id,
              titleColumnId: null,
              layout: 'continuous' as const,
              fit: 'width' as const,
              videoFit: 'full' as const,
              textLayer: true,
              activeRowId: null,
            };
      setDocumentView({
        ...base,
        sheetId: sheet.id,
        sourceColumnId: columnId,
        sync: false,
        activeRowId: rowId,
      });
      selection.clearRowSelection(sheet.id);
    },
    [sheet, documentView, selection, setDocumentView, setOpenSplit],
  );

  const openRowRef = useCallback(
    (sheetId: number | string, rowId: number | string) => {

      openRowRefTransition({ selection }, writeRoute, project.id, sheetId, rowId);
    },
    [project.id, selection, writeRoute],
  );

  const closeRoutePanel = useCallback(() => {

    closeRoutePanelTransition({ detail, selection }, writeRoute, routeStateBase());
  }, [detail, routeStateBase, selection, writeRoute]);

  const closeActionRoute = useCallback(() => {
    writeRoute(routeStateBase());
  }, [routeStateBase, writeRoute]);

  const closeActionDrawerOnLaunch = useCallback(() => {
    hideActionPanel();
    closeActionRoute();
  }, [hideActionPanel, closeActionRoute]);
  useEffect(() => {
    closeActionDrawerRef.current = closeActionDrawerOnLaunch;
  }, [closeActionDrawerOnLaunch]);

  const closeSplit = useCallback(() => {
    setOpenSplit(null);
  }, [setOpenSplit]);

  useEffect(() => {
    if (openSplit?.kind !== 'map' || !sheet || openSplit.sheetId !== sheet.id) return;
    const splitColumn = sheet.columns.find((column) => column.id === openSplit.columnId);
    if (splitColumn?.type === 'geo_point') return;
    setOpenSplit(null);
  }, [openSplit, setOpenSplit, sheet]);

  useEffect(() => {
    if (!visibleHeaderMenu) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest('[data-testid="grid-column-header-menu"]')) return;
      detail.closeHeaderMenu();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') detail.closeHeaderMenu();
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [detail, visibleHeaderMenu]);
  const openReview = useCallback((runId?: string) => {
    setReviewRunId(runId ?? null);
    writeRoute({ ...routeStateBase(), review: true });
  }, [writeRoute, routeStateBase]);
  const closeReview = useCallback(() => {
    setReviewRunId(null);
    // Review-close alone intentionally uses history.back().

    if (routeReview && window.history.length > 1) window.history.back();
    else writeRoute(routeStateBase());
  }, [routeReview, writeRoute, routeStateBase]);

  const actionPanelVisible = actionPanelOpen || !!routeActionKind;

  useEffect(() => {
    const onReveal = (e: Event) => {
      const { sheetId } = (e as CustomEvent).detail ?? {};
      if (sheetId) {
        selectSheet(String(sheetId));
        detail.closeDrawers({ clearChildFilter: true });
      }
    };
    const onRevealChildren = (e: Event) => {
      const { parentSheetId, parentRowId, parentRowIndex, childCount } =
        (e as CustomEvent).detail ?? {};
      const child = sheets.find((s) => s.parent?.sheetId === String(parentSheetId));
      if (!child) return;
      selectSheet(child.id);
      let nextChildFilter: ChildFilter | null = null;
      if (parentRowId != null && typeof childCount === 'number' && childCount > 0) {
        const parentSheet = sheets.find((s) => s.id === String(parentSheetId));
        nextChildFilter = {
          sheetId: child.id,
          parentSheetName: parentSheet?.name ?? 'parent sheet',
          parentRowId: String(parentRowId),
          parentRowIndex: typeof parentRowIndex === 'number' ? parentRowIndex : null,
          count: childCount,
        };
      }
      detail.closeDrawers();
      detail.setChildFilter(nextChildFilter);
    };
    window.addEventListener('frisket:reveal-row', onReveal);
    window.addEventListener('frisket:reveal-children', onRevealChildren);
    return () => {
      window.removeEventListener('frisket:reveal-row', onReveal);
      window.removeEventListener('frisket:reveal-children', onRevealChildren);
    };
  }, [detail, selectSheet, sheets]);

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
    [projectData, refreshHistory, showError],
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
  }, [detail, projectData, refreshHistory, refreshSheets, sheet, showError]);

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
  }, [clearDeleteRowsConfirm, deleteRowsConfirm, detail, projectData, refreshHistory, refreshSheets, selection, showError]);

  const currentSavedViewsSheetId = sheet?.id ?? null;
  const canCreateSavedView = currentSavedViewsSheetId !== null && canEditProject(project);

  const loadViewsForSheet = useCallback(
    async (sheetId: string) => {
      const numericSheetId = Number(sheetId);
      const storeSheetId = Number.isFinite(numericSheetId) ? numericSheetId : sheetId;
      const token = savedViews.beginViewsLoad(storeSheetId);
      try {
        const loadedViews = await projectApi.listViews(sheetId);
        savedViews.receiveViews(storeSheetId, token, loadedViews);
      } catch (error: unknown) {
        if (savedViews.isViewsLoadCurrent(storeSheetId, token)) {
          showError(error instanceof Error ? error.message : String(error));
        }
      }
    },
    [projectApi, savedViews, showError],
  );

  useEffect(() => {
    if (currentSavedViewsSheetId === null) {
      savedViews.resetForSheet(null);
      return;
    }
    const numericSheetId = Number(currentSavedViewsSheetId);
    savedViews.resetForSheet(
      Number.isFinite(numericSheetId) ? numericSheetId : currentSavedViewsSheetId,
    );
    void loadViewsForSheet(currentSavedViewsSheetId);
  }, [currentSavedViewsSheetId, loadViewsForSheet, savedViews]);

  const ensureViewsLoaded = useCallback(() => {
    if (currentSavedViewsSheetId === null) return Promise.resolve();
    return loadViewsForSheet(currentSavedViewsSheetId);
  }, [currentSavedViewsSheetId, loadViewsForSheet]);

  const startCreatingSavedView = useCallback(
    (defaultName = '') => {
      if (!canCreateSavedView) return;
      savedViews.startCreating();
      if (defaultName) savedViews.setViewName(defaultName);
    },
    [canCreateSavedView, savedViews],
  );

  const revealSavedViewsContribution = useCallback(() => {
    if (isContributionHidden(SAVED_VIEWS_CONTRIBUTION_ID)) {
      revealContribution(SAVED_VIEWS_CONTRIBUTION_ID);
    }
  }, [isContributionHidden, revealContribution]);

  const openSavedViews = useCallback(() => {
    revealSavedViewsContribution();
    openDiscover('Views');
    void ensureViewsLoaded();
  }, [ensureViewsLoaded, openDiscover, revealSavedViewsContribution]);

  const openNewSavedView = useCallback(
    (defaultName = '') => {
      if (!canCreateSavedView) return;
      startCreatingSavedView(defaultName);
      openSavedViews();
    },
    [canCreateSavedView, openSavedViews, startCreatingSavedView],
  );

  const currentViewInput = useCallback(
    (name: string, sheetId: string | number) => ({
      name,
      sheetId,
      filter: activeGridFilter ?? {},
      sort: activeGridSort ?? null,
      columns: currentViewColumns,
      column_groups: columnGroupSpecs.length > 0 ? columnGroupSpecs : null,
    }),
    [activeGridFilter, activeGridSort, columnGroupSpecs, currentViewColumns],
  );

  // Definition replacement is intentionally a complete capture.  Unlike
  // creation's convenience defaults, this shape has no omitted field whose
  // old persisted value could accidentally survive an explicit update.
  const currentViewDefinition = useCallback(
    () => ({
      filter: activeGridFilter ?? {},
      sort: activeGridSort ?? null,
      columns: currentViewColumns,
      column_groups: columnGroupSpecs.length > 0 ? columnGroupSpecs : null,
    }),
    [activeGridFilter, activeGridSort, columnGroupSpecs, currentViewColumns],
  );

  const saveCurrentView = useCallback(() => {
    const name = viewName.trim();
    if (!sheet || !name) return;
    const ticket = savedViews.beginEditorMutation();
    if (!ticket) return;
    void projectApi
      .saveView(currentViewInput(name, sheet.id))
      .then((saved) => {
        savedViews.completeEditorMutation(ticket, saved);
      })
      .catch((e: Error) => {
        savedViews.failEditorMutation(ticket);
        showError(e.message);
      });
  }, [currentViewInput, savedViews, sheet, showError, viewName]);

  const renameSavedView = useCallback(() => {
    const name = viewName.trim();
    if (savedViewsEditor?.kind !== 'edit' || !name) return;
    const ticket = savedViews.beginEditorMutation();
    if (!ticket) return;
    void projectApi
      .renameView(savedViewsEditor.view.id, { name })
      .then((updated) => {
        savedViews.completeEditorMutation(ticket, updated);
      })
      .catch((e: Error) => {
        savedViews.failEditorMutation(ticket);
        showError(e.message);
      });
  }, [savedViews, savedViewsEditor, showError, viewName]);

  const startSavedViewDefinitionUpdate = useCallback(
    (view: SavedView) => {
      if (!canCreateSavedView || view.sheet_id === null || String(view.sheet_id) !== sheet?.id) return;
      savedViews.openDefinitionUpdate(view);
    },
    [canCreateSavedView, savedViews, sheet?.id],
  );

  const replaceSavedViewDefinition = useCallback(() => {
    const confirmation = savedViews.store.get().confirmation;
    if (confirmation?.kind !== 'update-definition' || !canCreateSavedView || !sheet) return;
    const ticket = savedViews.beginConfirmationMutation();
    if (!ticket) return;
    void projectApi
      .replaceViewDefinition(confirmation.view.id, currentViewDefinition())
      .then((updated) => {
        savedViews.completeDefinitionUpdate(ticket, updated);
      })
      .catch((error: unknown) => {
        savedViews.failConfirmationMutation(
          ticket,
          error instanceof Error ? error.message : String(error),
        );
      });
  }, [canCreateSavedView, currentViewDefinition, projectApi, savedViews, sheet]);

  const startSavedViewDelete = useCallback(
    (view: SavedView) => {
      if (!canCreateSavedView || view.sheet_id === null || String(view.sheet_id) !== sheet?.id) return;
      savedViews.openDeleteConfirmation(view);
    },
    [canCreateSavedView, savedViews, sheet?.id],
  );

  const deleteView = useCallback(() => {
    const confirmation = savedViews.store.get().confirmation;
    if (confirmation?.kind !== 'delete' || !canCreateSavedView) return;
    const ticket = savedViews.beginConfirmationMutation();
    if (!ticket) return;
    void projectApi
      .deleteView(confirmation.view.id)
      .then(() => {
        savedViews.completeDeleteMutation(ticket);
      })
      .catch((error: unknown) => {
        savedViews.failConfirmationMutation(
          ticket,
          error instanceof Error ? error.message : String(error),
        );
      });
  }, [canCreateSavedView, projectApi, savedViews]);

  const addWatchForCurrentView = useCallback(() => {
    if (!sheet || !canEditProject(project)) return;
    const definition = currentViewInput('', sheet.id);
    const sheetId = Number(definition.sheetId);
    watchRunLink.setPendingCreateDraft({
      query: {
        kind: 'filter',
        sheet_id: sheetId,
        filter: definition.filter,
      },
      scope: { kind: 'sheet', sheet_id: sheetId },
      name: `Watch: ${sheet.name}`,
    });
    if (isContributionHidden(WATCHES_CONTRIBUTION_ID)) {
      revealContribution(WATCHES_CONTRIBUTION_ID);
    }
    openDiscover('Watches');
  }, [currentViewInput, isContributionHidden, openDiscover, project, revealContribution, sheet, watchRunLink]);

  const applySavedView = useCallback(
    (view: SavedView) => {
      if (!sheet || String(view.sheet_id) !== sheet.id) return;
      const filter = normalizeGridFilterSpec(view.spec.filter);
      const sort = normalizeGridSortSpec(view.spec.sort);
      const rawColumns = Array.isArray(view.spec.columns)
        ? view.spec.columns.filter((name): name is string => typeof name === 'string')
        : null;
      const groups = Array.isArray(view.spec.column_groups)
        ? (view.spec.column_groups as ColumnGroupViewSpec[])
        : [];
      const layouts = Object.fromEntries(
        groups.map((group) => [
          String(group.run_id),
          {
            label: group.label,
            showConfidence: group.show_confidence,
            showJustification: group.show_justification,
          },
        ]),
      );
      const key = `frisket:column-groups:${project.id}:${sheet.id}`;
      if (groups.length === 0) {
        localStorage.removeItem(key);
      } else {
        localStorage.setItem(key, JSON.stringify(layouts));
      }
      const columns = rawColumns === null
        ? normalizeColumnOrder(null, sheet)
        : normalizeColumnOrder(rawColumns, sheet);
      const hiddenColumns = rawColumns === null
        ? []
        : (() => {
            const groupHidden = groupManagedHiddenColumnNames(groups);
            return exactViewHiddenColumns(sheet, rawColumns, groupHidden);
          })();
      if (rawColumns === null) {
        localStorage.removeItem(columnOrderKey(project.id, sheet.id));
        localStorage.removeItem(hiddenColumnsKey(project.id, sheet.id));
      } else {
        localStorage.setItem(columnOrderKey(project.id, sheet.id), JSON.stringify(columns));
        localStorage.setItem(hiddenColumnsKey(project.id, sheet.id), JSON.stringify(hiddenColumns));
      }
      const firstColumnName = sheet.columns[0]?.name ?? '';
      let draftSortColumn = firstColumnName;
      let draftSortDirection: GridSortDirection = 'asc';
      if (sort?.[0]) {
        draftSortColumn = sort[0].column;
        draftSortDirection = sort[0].dir;
      }

      applySavedViewTransition({ gridView, detail, selection }, sheet.id, {
        viewId: view.id,
        sheetId: sheet.id,
        filter,
        sort,
        draftSortColumn,
        draftSortDirection,
        columns,
        hiddenColumns,
        columnGroupSpecs: groups,
      });
      projectData.invalidate();
    },
    [detail, gridView, project.id, projectData, selection, sheet],
  );

  // Block on a stale or incomplete runtime index.

  const applyLens = useCallback(
    async (lensId: number, name: string) => {
      if (!sheet) return;
      const resolveGeneration = lensViewHandle.beginLensResolve();
      lensViewHandle.setLensOpenError(null);
      let resolved: LensResolved;
      try {
        resolved = await projectApi.resolveLens(lensId, { limit: MAX_LENS_VIEW_ROWS });
      } catch (e) {
        if (!lensViewHandle.isCurrentLensResolve(resolveGeneration)) return;
        if (e instanceof ApiError && LENS_REFRESH_NEEDED_CODES.has(e.code ?? '')) {
          lensViewHandle.setLensView(null);
          lensViewHandle.setLensOpenError('This view is out of date — refresh the index before opening it.');
        } else {
          const message = e instanceof Error ? e.message : String(e);
          lensViewHandle.setLensView(null);
          lensViewHandle.setLensOpenError(message);
          showError(message);
        }
        return;
      }
      if (!lensViewHandle.isCurrentLensResolve(resolveGeneration)) return;

      applyLensTransition(
        { gridView, detail, lensView: lensViewHandle },
        {
          lensId,
          name,
          sheetId: sheet.id,
          rowIds: resolved.rowIds,
          scores: resolved.scores,
          total: resolved.total,
        },
      );
      projectData.invalidate();
    },
    [detail, gridView, lensViewHandle, projectData, sheet, showError],
  );

  const exitLensView = useCallback(() => {
    lensViewHandle.beginLensResolve();
    lensViewHandle.setLensView(null);
    lensViewHandle.setLensOpenError(null);
    projectData.invalidate();
  }, [lensViewHandle, projectData]);

  // Drop stale preview results by generation.
  const openPreviewView = useCallback(
    async (req: ActionExecutionRequest, onComplete?: (result: PreviewSampleResult) => void) => {
      if (detail.store.get().rowDrawerPreview) closeRoutePanel();
      await previewViewHandle.openPreviewView(req, sheet ?? null, {
        invalidateProjectData: projectData.invalidate,
        requestCostConfirmation,
        onComplete,
      });
    },
    [previewViewHandle, projectData, requestCostConfirmation, sheet, detail, closeRoutePanel],
  );

  // Closing in-memory preview state must not touch history.
  const closePreviewView = useCallback(() => {
    teardownPreviewJob();
    previewViewHandle.clearPreviewView();
    if (detail.store.get().rowDrawerPreview) closeRoutePanel();
  }, [previewViewHandle, teardownPreviewJob, detail, closeRoutePanel]);

  const runPreviewForReal = useCallback(() => {
    if (!activePreviewView) return;
    const runReq = { ...activePreviewView.req } as ActionExecutionRequest;
    if ('action_id' in runReq) {
      runReq.idempotency_key = freshActionRequestKey(runReq.action_id);
    } else if ('previewRows' in runReq) {
      delete runReq.previewRows;
    }
    closePreviewView();
    startRun(runReq);
  }, [activePreviewView, closePreviewView, startRun]);

  const runWithPreview = useCallback(
    (req: ActionExecutionRequest) => {
      if ('previewRows' in req && req.previewRows) void openPreviewView(req);
      else {
        if (activePreviewView) {
          closePreviewView();
        }
        startRun(req);
      }
    },
    [
      activePreviewView,
      openPreviewView,
      closePreviewView,
      startRun,
    ],
  );

  const executeRegisteredAction = useCallback(
    (req: RegisteredActionRequest, intent: 'preview' | 'run',
      onPreviewComplete?: (result: PreviewSampleResult) => void) => {
      if (intent === 'preview') {
        void openPreviewView(req, onPreviewComplete);
        return;
      }
      if (activePreviewView) {
        closePreviewView();
      }
      startRun(req);
    },
    [activePreviewView, openPreviewView, closePreviewView, startRun],
  );

  // Retries replay durable results free and charge unfinished rows.

  // Cost-gate cancellation must not mutate or error.

  const runColumnBackfill = useCallback(
    (sheetId: string, targetColumnName: string, rowIds?: number[]) =>
      backfillWithConfirmation(projectApi, requestCostConfirmation, sheetId, targetColumnName, rowIds)
        .then((result) => {
          if (result) afterBackfill(result.runId);
        })
        .catch((e: unknown) => showError(e instanceof Error ? e.message : String(e))),
    [afterBackfill, requestCostConfirmation, showError],
  );

  const runActionBackfill = useCallback(
    (targetColumnName: string, rowIds?: number[]) => {
      if (!sheet) return Promise.resolve();
      return runColumnBackfill(sheet.id, targetColumnName, rowIds);
    },
    [runColumnBackfill, sheet],
  );

  const resumeHaltedRun = runColumnBackfill;

  const retryFailedRows = useCallback(
    async (targetColumnName: string, outcome: string): Promise<void> => {
      if (!sheet) return;
      try {
        const filter: GridFilterSpec = { [targetColumnName]: { failed: outcome } };
        const pageSize = 1000;
        const rowIds: number[] = [];
        for (let offset = 0; ; ) {
          const page = await projectApi.getSheetData(sheet.id, offset, pageSize, { filter });
          for (const row of page.rows) rowIds.push(Number(row.id));
          offset += page.rows.length;
          if (page.rows.length === 0 || offset >= page.total) break;
        }
        if (rowIds.length === 0) return;
        const result = await backfillWithConfirmation(
          projectApi,
          requestCostConfirmation,
          sheet.id,
          targetColumnName,
          rowIds,
        );
        if (result) afterBackfill(result.runId);
      } catch (e: unknown) {
        showError(e instanceof Error ? e.message : String(e));
      }
    },
    [afterBackfill, requestCostConfirmation, sheet, showError],
  );

  const applyGridFilterForColumn = useCallback(
    (
      columnName: string,
      operatorIn: GridFilterOperator,
      rawValue: string,
      rawStart = '',
      rawEnd = '',
    ) => {
      if (!sheet || !columnName) return;
      const selectedColumn = sheet.columns.find((column) => column.name === columnName);
      const operator = filterOperatorForColumnType(selectedColumn?.type, operatorIn);
      const value = rawValue.trim();
      const start = rawStart.trim();
      const end = rawEnd.trim();
      const next: GridFilterSpec = {
        [columnName]: filterConditionFromDraft(operator, value, start, end),
      };

      applyGridFilterTransition({ gridView, detail, selection }, sheet.id, {
        filter: next,
      });

    },
    [detail, gridView, selection, sheet],
  );

  const applyGridBboxFilter = useCallback(
    (columnName: string, bbox: [number, number, number, number]) => {
      if (!sheet || !columnName) return;
      const value: GridFilterBboxValue = {
        min_lon: bbox[0],
        min_lat: bbox[1],
        max_lon: bbox[2],
        max_lat: bbox[3],
      };

      applyGridBboxFilterTransition(
        { gridView, detail, selection, workView },
        { setOpenSplit, setDocumentView, setPromotedViews },
        { promotedViews, activePromotedKey },
        sheet.id,
        {
          filter: { [columnName]: { bbox: value } },
        },
      );
    },
    [
      activePromotedKey,
      detail,
      gridView,
      promotedViews,
      workView,
      selection,
      setDocumentView,
      setOpenSplit,
      setPromotedViews,
      sheet,
    ],
  );

  const applyGridFilterSpec = useCallback(
    (filter: GridFilterSpec | null) => {
      if (!sheet) return;
      const [columnName, condition] = Object.entries(filter ?? {})[0] ?? [];
      if (!columnName || !condition) {
        clearGridFilterTransition({ gridView, selection }, sheet.id);
        return;
      }
      const previous = gridView.store.get().applied;
      const preservesEntityLabel = previous.filterValueLabel !== null
        && Object.entries(previous.filter ?? {}).some(([column, previousCondition]) => (
          previousCondition.entity_eq !== undefined
          && JSON.stringify(filter?.[column]?.entity_eq) === JSON.stringify(previousCondition.entity_eq)
        ));
      applyGridFilterTransition({ gridView, detail, selection }, sheet.id, {
        filter: filter ?? {},
        filterValueLabel: preservesEntityLabel ? previous.filterValueLabel : null,
      });
    },
    [detail, gridView, selection, sheet],
  );

  const applyGridFailedFilter = useCallback(
    (columnName: string, outcome: string) => {
      if (!sheet || !columnName) return;
      applyGridBboxFilterTransition(
        { gridView, detail, selection, workView },
        { setOpenSplit, setDocumentView, setPromotedViews },
        { promotedViews, activePromotedKey },
        sheet.id,
        {
          filter: { [columnName]: { failed: outcome } },
        },
      );
    },
    [
      activePromotedKey,
      detail,
      gridView,
      promotedViews,
      workView,
      selection,
      setDocumentView,
      setOpenSplit,
      setPromotedViews,
      sheet,
    ],
  );

  const applyGridEntityFilter = useCallback(
    (columnName: string, value: GridFilterEntityValue, valueLabel?: string) => {
      if (!sheet || !columnName) return;
      applyGridBboxFilterTransition(
        { gridView, detail, selection, workView },
        { setOpenSplit, setDocumentView, setPromotedViews },
        { promotedViews, activePromotedKey },
        sheet.id,
        {
          filter: { [columnName]: { entity_eq: value } },
          filterValueLabel: valueLabel ?? null,
        },
      );
    },
    [
      activePromotedKey,
      detail,
      gridView,
      promotedViews,
      workView,
      selection,
      setDocumentView,
      setOpenSplit,
      setPromotedViews,
      sheet,
    ],
  );

  const applyGridSortForColumn = useCallback(
    (columnName: string, direction: GridSortDirection) => {
      if (!sheet || !columnName) return;

      applyGridSortTransition({ gridView, detail, selection }, sheet.id, {
        column: columnName,
        direction,
        sort: [{ column: columnName, dir: direction }],
      });
    },
    [detail, gridView, selection, sheet],
  );

  const sortHeaderColumn = useCallback(
    (column: ColumnDef) => {
      const current = activeGridSort?.find((rule) => rule.column === column.name);
      const direction: GridSortDirection = current?.dir === 'asc' ? 'desc' : 'asc';
      applyGridSortForColumn(column.name, direction);
    },
    [activeGridSort, applyGridSortForColumn],
  );

  const applyInlineFilter = useCallback(
    (columnName: string, rawValue: string) => {
      if (!sheet || !columnName) return;
      const value = rawValue;
      const base: GridFilterSpec = { ...(activeGridFilter ?? {}) };
      if (value.trim() === '') delete base[columnName];
      else base[columnName] = { contains: value };

      if (Object.keys(base).length === 0) {
        clearGridFilterTransition({ gridView, selection }, sheet.id);
      } else {
        applyGridFilterTransition({ gridView, detail, selection }, sheet.id, {
          filter: base,
        });
      }
    },
    [detail, gridView, selection, sheet, activeGridFilter],
  );

  const openGridHeaderMenu = useCallback(
    ({ column, columnIndex, bounds }: HeaderMenuState) => {
      detail.openHeaderMenu({ column, columnIndex, bounds });
    },
    [detail],
  );

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

  const applyGridSort = useCallback(() => {
    applyGridSortForColumn(effectiveSortColumn, draftSortDirection);
  }, [applyGridSortForColumn, draftSortDirection, effectiveSortColumn]);

  const clearGridFilter = useCallback(() => {

    clearGridFilterTransition({ gridView, selection }, sheet?.id);
  }, [gridView, selection, sheet]);

  const clearGridSort = useCallback(() => {
    clearGridSortTransition({ gridView, selection }, sheet?.id);
  }, [gridView, selection, sheet]);

  const headerMenuActiveSort = useMemo(() => {
    if (!headerMenu) return null;
    return activeGridSort?.find((item) => item.column === headerMenu.column.name) ?? null;
  }, [activeGridSort, headerMenu]);

  const headerMenuActiveFilter = useMemo(() => {
    if (!headerMenu) return null;
    return activeGridFilter?.[headerMenu.column.name] ?? null;
  }, [activeGridFilter, headerMenu]);

  const changeColumnOrder = useCallback(
    (columns: string[]) => {
      if (!sheet) return;
      const next = normalizeColumnOrder(columns, sheet);
      gridView.setColumnOrder(sheet.id, next);
      localStorage.setItem(columnOrderKey(project.id, sheet.id), JSON.stringify(next));
    },
    [gridView, project.id, sheet],
  );

  const hiddenColumnNames = activeDisplayHiddenColumns;

  const headerMenuAdjacentHidden = useMemo(() => {
    if (!headerMenu) return [];
    const userHidden = new Set(activeHiddenColumns);
    const index = activeColumnOrder.indexOf(headerMenu.column.name);
    if (index === -1) return [];
    const run: string[] = [];
    for (let i = index - 1; i >= 0 && userHidden.has(activeColumnOrder[i]); i -= 1) {
      run.unshift(activeColumnOrder[i]);
    }
    for (let i = index + 1; i < activeColumnOrder.length && userHidden.has(activeColumnOrder[i]); i += 1) {
      run.push(activeColumnOrder[i]);
    }
    return run;
  }, [activeColumnOrder, activeHiddenColumns, headerMenu]);

  const visibleOrderedColumnNames = useMemo(() => {
    if (!sheet) return [];
    const hidden = new Set(hiddenColumnNames);
    return activeColumnOrder.filter((name) => !hidden.has(name));
  }, [activeColumnOrder, hiddenColumnNames, sheet]);
  const visibleOrderedColumns = useMemo(() => {
    if (!sheet) return [];
    const byName = new Map(sheet.columns.map((column) => [column.name, column]));
    return visibleOrderedColumnNames
      .map((name) => byName.get(name))
      .filter((column): column is ColumnDef => column !== undefined);
  }, [sheet, visibleOrderedColumnNames]);

  const activeFrozenColumnCount = useMemo(() => {
    if (!sheet) return 0;
    return clampFrozenColumnCount(
      frozenColumnCountBySheet[sheet.id] ??
        loadFrozenColumnCount(project.id, sheet.id, visibleOrderedColumnNames.length),
      visibleOrderedColumnNames.length,
    );
  }, [frozenColumnCountBySheet, project.id, sheet, visibleOrderedColumnNames.length]);

  const changeFrozenColumnCount = useCallback(
    (count: number) => {
      if (!sheet) return;
      const next = clampFrozenColumnCount(count, visibleOrderedColumnNames.length);
      gridView.setFrozenColumnCount(sheet.id, next);
      localStorage.setItem(frozenColumnsKey(project.id, sheet.id), String(next));
      closeHeaderMenu();
    },
    [closeHeaderMenu, gridView, project.id, sheet, visibleOrderedColumnNames.length],
  );

  const setHiddenColumns = useCallback(
    (updater: (previous: string[]) => string[]) => {
      if (!sheet) return;
      const previous = hiddenColumnsBySheet[sheet.id] ?? loadHiddenColumns(project.id, sheet);
      const known = new Set(sheet.columns.map((col) => col.name));
      const seen = new Set<string>();
      const next = updater(previous).filter(
        (name) => known.has(name) && !seen.has(name) && (seen.add(name), true),
      );
      const nextSet = new Set(next);
      const newlyShownDefaults = previous.filter(
        (name) => !nextSet.has(name) && sheet.columns.some((column) => column.name === name && column.defaultHidden),
      );
      if (newlyShownDefaults.length) {
        const key = shownDefaultColumnsKey(project.id, sheet.id);
        const stored = localStorage.getItem(key);
        const shown = new Set<string>(stored ? JSON.parse(stored) as string[] : []);
        for (const name of newlyShownDefaults) shown.add(name);
        localStorage.setItem(key, JSON.stringify([...shown]));
      }
      gridView.setHiddenColumns(sheet.id, next);
      localStorage.setItem(hiddenColumnsKey(project.id, sheet.id), JSON.stringify(next));
    },
    [gridView, hiddenColumnsBySheet, project.id, sheet],
  );

  const hideColumn = useCallback(
    (name: string) => {
      setHiddenColumns((previous) => [...previous, name]);
      closeHeaderMenu();
    },
    [closeHeaderMenu, setHiddenColumns],
  );

  const setRowTitleColumn = useCallback(
    (columnId: string) => {
      if (!sheet) return;
      closeHeaderMenu();
      void projectApi
        .setSheetTitleColumn(sheet.id, columnId)
        .then(() => refreshSheets())
        .catch((e: Error) => showError(e.message));
    },
    [sheet, projectApi, refreshSheets, showError, closeHeaderMenu],
  );

  const revealColumns = useCallback(
    (names: string[]) => {
      const drop = new Set(names);
      setHiddenColumns((previous) => previous.filter((name) => !drop.has(name)));
      closeHeaderMenu();
    },
    [closeHeaderMenu, setHiddenColumns],
  );

  const revealAllHiddenColumns = useCallback(() => {
    setHiddenColumns(() => []);
  }, [setHiddenColumns]);

  const openAddColumnPrompt = useCallback((anchor: { x: number; y: number }) => {
    actSurface.openAddColumnPromptAt(anchor);
  }, [actSurface]);

  const closeAddColumnPrompt = useCallback(
    () => actSurface.closeAddColumnPrompt(),
    [actSurface],
  );

  const insertColumnBeside = useCallback(
    (side: 'left' | 'right') => {
      if (!sheet || !headerMenu) return;
      const index = sheet.columns.findIndex((col) => col.id === headerMenu.column.id);
      const position = index === -1 ? null : index + 1 + (side === 'right' ? 1 : 0);

      insertColumnBesideTransition(
        { detail, actSurface },
        {
          position,
          anchor: {
            x: headerMenu.bounds.x,
            y: headerMenu.bounds.y + headerMenu.bounds.height,
          },
          anchorName: headerMenu.column.name,
          side,
        },
      );
    },
    [actSurface, detail, headerMenu, sheet],
  );

  const submitAddColumn = useCallback(
    async (name: string, type: string) => {
      if (!sheet) return;
      const trimmed = name.trim();
      if (!trimmed) return;
      const position = addColumnPrompt?.position ?? null;
      const anchorName = addColumnPrompt?.anchorName ?? null;
      const side = addColumnPrompt?.side ?? 'right';
      try {
        const added = await projectApi.addColumn(sheet.id, trimmed, { type, position });
        const next = anchorName
          ? spliceColumnOrderBeside(activeColumnOrder, added.name, anchorName, side)
          : [...activeColumnOrder.filter((column) => column !== added.name), added.name];
        completeAddColumnTransition({ gridView, actSurface }, sheet.id, next);
        localStorage.setItem(columnOrderKey(project.id, sheet.id), JSON.stringify(next));
        projectData.invalidate();
        await refreshSheets();
        void refreshHistory();
      } catch (e: unknown) {
        showError(e instanceof Error ? e.message : String(e));
      }
    },
    [actSurface, activeColumnOrder, addColumnPrompt, gridView, project.id, projectData, refreshHistory, refreshSheets, sheet, showError],
  );

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
            const normalized = projectData.normalize(route.store.get());
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
  const mapHidden =
    mapContributionId !== null && isContributionHidden(mapContributionId);
  const graphNeighborhoodHidden = isContributionHidden(GRAPH_NEIGHBORHOOD_CONTRIBUTION_ID);
  const evidenceMainViewHidden = isContributionHidden(EVIDENCE_CONTRIBUTION_ID);
  const sourceHealthHidden = isContributionHidden(SOURCE_HEALTH_CONTRIBUTION_ID);
  const mapColumn =
    sheet && openSplit?.kind === 'map' && openSplit.sheetId === sheet.id && mapViewDescriptor && !mapHidden
      ? sheet.columns.find((c) => c.id === openSplit.columnId && c.type === 'geo_point') ?? null
      : null;
  const firstGeoColumn = sheet?.columns.find((column) => column.type === 'geo_point') ?? null;

  const firstMediaColumn = sheet ? documentMediaColumns(sheet)[0] ?? null : null;

  const sheetIsEdgeShaped =
    sheet?.materializedKind === 'edge' || sheet?.materializedKind === 'join';

  const citedColumnIds = sheet ? sheet.citedColumnIds : NO_CITED_COLUMNS;

  const annotatedTextColumnIds = sheet ? sheet.annotatedTextColumnIds : NO_CITED_COLUMNS;

  const imageGalleryAvailability = useMemo(
    () => resolvePluginViewAvailability(IMAGE_GALLERY_VIEW_DESCRIPTOR, sheet ?? null),
    [sheet],
  );

  const workViewAvailability = useMemo(
    () =>
      selectWorkViewAvailability({
        sheet: sheet ?? null,
        hiddenContributionIds: hiddenContributionIdSet,
        mapContributionId,
        contributionIds: { graphNeighborhood: GRAPH_NEIGHBORHOOD_CONTRIBUTION_ID },
        firstGeoColumn,
        firstMediaColumn,
        imageGalleryAvailable: imageGalleryAvailability.available,
        sheetIsEdgeShaped,
        citedColumnIds,
        annotatedTextColumnIds,
      }),
    [
      sheet,
      hiddenContributionIdSet,
      mapContributionId,
      firstGeoColumn,
      firstMediaColumn,
      imageGalleryAvailability.available,
      sheetIsEdgeShaped,
      citedColumnIds,
      annotatedTextColumnIds,
    ],
  );
  const documentAvailable = workViewAvailability.document.available;
  const answersAvailable = workViewAvailability.answers.available;
  const mapAvailable = workViewAvailability.map.available;
  const mapAvailabilityReason = workViewAvailability.map.reason;
  const mapAvailabilityStatus = workViewAvailability.map.status;

  const answersCitedColumns = useMemo(
    () =>
      sheet
        ? sheet.columns.filter((column) => citedColumnIds.includes(String(column.id)))
        : NO_CITED_COLUMNS_RESOLVED,
    [sheet, citedColumnIds],
  );

  const gridOnly = Boolean(sheet) && gridOnlySheetId === sheet?.id;

  const galleryPaneShowing =
    Boolean(sheet) &&
    !gridOnly &&
    !isContributionHidden(IMAGE_GALLERY_VIEW_DESCRIPTOR.id) &&
    imageGalleryAvailability.available;
  const mapSplitShowing = openSplit?.kind === 'map' && openSplit.sheetId === sheet?.id && mapColumn !== null;
  const graphSplitShowing =
    openSplit?.kind === 'graph' &&
    openSplit.sheetId === sheet?.id &&
    !graphNeighborhoodHidden &&
    sheetIsEdgeShaped;

  const documentViewShowing =
    documentView !== null && documentView.sheetId === sheet?.id && documentAvailable;

  const answersViewShowing =
    answersViewSheetId !== null && answersViewSheetId === sheet?.id && answersAvailable;

  const activePromotedView = useMemo<PromotedView | null>(() => {
    if (!activePromotedKey || !sheet) return null;
    const view = promotedViews.find((candidate) => candidate.key === activePromotedKey);
    return view && view.sheetId === sheet.id ? view : null;
  }, [activePromotedKey, promotedViews, sheet]);

  const activeWorkView: WorkViewKind = documentViewShowing
    ? 'document'
    : answersViewShowing
      ? 'answers'
      : mapSplitShowing
        ? 'map'
        : graphSplitShowing
          ? 'graph'
          : galleryPaneShowing
            ? 'gallery'
            : 'grid';

  const workViewSegments = useMemo(
    () =>
      Object.fromEntries(
        WORK_VIEW_KINDS.map((kind) => [kind, workViewAvailability[kind].available]),
      ) as Record<WorkViewKind, boolean>,
    [workViewAvailability],
  );
  const workViewDisabledReasons = useMemo<Partial<Record<WorkViewKind, string>>>(() => {
    const reasons: Partial<Record<WorkViewKind, string>> = {};
    if (!mapAvailable && firstGeoColumn !== null) {
      reasons.map =
        mapContributionId === null
          ? 'Map view unavailable: the map plugin contribution did not load'
          : 'Map view hidden — reveal it via the ⌘⇧P palette';
    }
    if (sheetIsEdgeShaped && graphNeighborhoodHidden) {
      reasons.graph = 'Graph view hidden — reveal it via the ⌘⇧P palette';
    }
    return reasons;
  }, [mapAvailable, firstGeoColumn, mapContributionId, sheetIsEdgeShaped, graphNeighborhoodHidden]);

  const setWorkView = useCallback(
    (kind: WorkViewKind) => {
      setWorkViewResetTransition({ setDocumentView }, workView, compareView, kind);
      const sheetId = sheet?.id ?? null;
      if (sheetId && kind !== activeWorkView) selection.clearRowSelection(sheetId);
      switch (kind) {
        case 'grid':
          setWorkViewGridTransition({ setOpenSplit }, workView, sheetId);
          break;
        case 'document':
          setWorkViewDocumentTransition(
            { setOpenSplit, setDocumentView },
            workView,
            sheetId,
            sheetId
              ? documentView && documentView.sheetId === sheetId
                ? { ...documentView, sync: false }
                : {
                    sheetId,
                    sourceColumnId: null,
                    titleColumnId: null,
                    layout: 'continuous',
                    fit: 'width',
                    videoFit: 'full',
                    textLayer: true,
                    sync: false,
                    activeRowId: null,
                  }
              : null,
          );
          break;
        case 'answers':
          setWorkViewAnswersTransition({ setOpenSplit }, workView, sheetId);
          break;
        case 'map':
          workView.setGridOnlySheetId(null);
          if (firstGeoColumn) openMapPanel(firstGeoColumn);
          break;
        case 'gallery':
          setWorkViewGalleryTransition({ setOpenSplit }, workView);
          break;
        case 'graph':
          workView.setGridOnlySheetId(null);
          openGraphPanel();
          break;
      }
    },
    [
      setDocumentView,
      setOpenSplit,
      firstGeoColumn,
      openMapPanel,
      openGraphPanel,
      workView,
      compareView,
      documentView,
      activeWorkView,
      selection,
      sheet?.id,
    ],
  );

  const promoteCurrentView = useCallback(() => {
    if (!sheet || activeWorkView === 'grid' || activeWorkView === 'answers') return;
    const kind = activeWorkView;
    const columnId =
      kind === 'map' ? mapColumn?.id ?? firstGeoColumn?.id ?? undefined : undefined;
    const key = `${kind}:${sheet.id}:${columnId ?? ''}`;
    const label = `${WORK_VIEW_TITLES[kind]} of ${sheet.name}`;

    promoteCurrentViewTransition({ setOpenSplit, setPromotedViews }, workView, promotedViews, {
      key,
      sheetId: sheet.id,
      kind,
      columnId,
      label,
    });
  }, [sheet, activeWorkView, mapColumn, firstGeoColumn, promotedViews, setOpenSplit, setPromotedViews, workView]);

  const openPromotedTab = useCallback(
    (view: PromotedView) => {

      openPromotedTabTransition({ setOpenSplit }, workView, compareView);
      if (sheet?.id !== view.sheetId) selectSheet(view.sheetId);
      workView.setActivePromotedKey(view.key);
    },
    [setOpenSplit, workView, compareView, sheet?.id, selectSheet],
  );

  const openOcrCompareTab = useCallback((target: OcrCompareTarget | null = null) => {
    setOcrCompareTarget(target);
    openCompareTabTransition({ setOpenSplit }, workView, compareView, 'ocr');
  }, [workView, compareView, setOpenSplit]);
  const closeOcrCompareTab = useCallback(() => {
    compareView.close('ocr');
    setOcrCompareTarget(null);
  }, [compareView]);
  const setOcrCompareSession = useCallback(
    (session: CompareTabSession) => compareView.setSession('ocr', session),
    [compareView],
  );
  const setOcrCompareCloseWarn = useCallback(
    (open: boolean) => compareView.setCloseWarn('ocr', open),
    [compareView],
  );

  const openTranscribeCompareTab = useCallback(() => {

    openCompareTabTransition({ setOpenSplit }, workView, compareView, 'transcribe');
  }, [workView, compareView, setOpenSplit]);
  const closeTranscribeCompareTab = useCallback(() => {
    compareView.close('transcribe');
  }, [compareView]);
  const setTranscribeCompareSession = useCallback(
    (session: CompareTabSession) => compareView.setSession('transcribe', session),
    [compareView],
  );
  const setTranscribeCompareCloseWarn = useCallback(
    (open: boolean) => compareView.setCloseWarn('transcribe', open),
    [compareView],
  );

  const openTranslateCompareTab = useCallback(() => {
    openCompareTabTransition({ setOpenSplit }, workView, compareView, 'translate');
  }, [workView, compareView, setOpenSplit]);
  const closeTranslateCompareTab = useCallback(() => {
    compareView.close('translate');
  }, [compareView]);
  const setTranslateCompareSession = useCallback(
    (session: CompareTabSession) => compareView.setSession('translate', session),
    [compareView],
  );
  const setTranslateCompareCloseWarn = useCallback(
    (open: boolean) => compareView.setCloseWarn('translate', open),
    [compareView],
  );

  const openTopicCompareTab = useCallback(() => {
    openCompareTabTransition({ setOpenSplit }, workView, compareView, 'topic');
  }, [workView, compareView, setOpenSplit]);
  const closeTopicCompareTab = useCallback(() => {
    compareView.close('topic');
  }, [compareView]);
  const setTopicCompareSession = useCallback(
    (session: CompareTabSession) => compareView.setSession('topic', session),
    [compareView],
  );
  const setTopicCompareCloseWarn = useCallback(
    (open: boolean) => compareView.setCloseWarn('topic', open),
    [compareView],
  );

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

  const closePromotedTab = useCallback(
    (view: PromotedView) => {

      const wasActive = closePromotedTabTransition(
        { setPromotedViews },
        workView,
        promotedViews,
        view,
        activePromotedKey,
      );
      if (wasActive && sheet?.id !== view.sheetId) selectSheet(view.sheetId);
    },
    [promotedViews, setPromotedViews, activePromotedKey, workView, sheet?.id, selectSheet],
  );

  const selectSheetTab = useCallback(
    (sheetId: string) => {
      selectSheetTabTransition(workView, compareView);
      selectSheet(sheetId);
    },
    [workView, compareView, selectSheet],
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
    () =>
      resolveRowCacheScope({
        parentRowId: activeChildFilter?.parentRowId,
        filter: activeGridFilter,
        sort: activeGridSort,
        lensRowIds: activeGridLensRowIds,
      }),
    [activeChildFilter?.parentRowId, activeGridFilter, activeGridSort, activeGridLensRowIds],
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

  const activityRailPluginDescriptors = useMemo(
    () =>
      [
        ...pluginPanelDescriptorsFromRuntimeIndex(workbenchPluginRuntimeIndex),
        ...pluginViewDescriptorsFromRuntimeIndex(workbenchPluginRuntimeIndex),
      ].filter((descriptor) =>
        descriptor.placements.some((placement) => placement.host === 'activityRail'),
      ),
    [workbenchPluginRuntimeIndex],
  );
  const pluginLauncherSpecs = useMemo<RibbonPluginLauncher[]>(
    () =>
      activityRailPluginDescriptors.map((descriptor) => ({
        contributionId: descriptor.id,
        label: descriptor.shortTitle ?? descriptor.title,
        iconName: descriptor.icon,
      })),
    [activityRailPluginDescriptors],
  );

  const actRibbonTabs = useMemo(
    () => resolveRibbonTabs(actActionTemplates, workbenchDataRequirementContext, pluginLauncherSpecs),
    [actActionTemplates, workbenchDataRequirementContext, pluginLauncherSpecs],
  );

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

  const mainViewPluginViewDescriptors = useMemo(
    () =>
      pluginViewDescriptorsFromRuntimeIndex(
        workbenchPluginRuntimeIndex,
      ).filter((descriptor) =>
        descriptor.placements.some((placement) => placement.host === 'mainView'),
      ),
    [workbenchPluginRuntimeIndex],
  );
  const mainViewPluginViewAvailability = useMemo(
    () =>
      mainViewPluginViewDescriptors.map((descriptor) => ({
        descriptor,
        availability: isProjectionViewDescriptor(descriptor)
          ? resolvePluginProjectionViewAvailability(descriptor, sheet ?? null)
          : resolvePluginViewAvailability(descriptor, sheet ?? null),
      })),
    [mainViewPluginViewDescriptors, sheet],
  );
  const activeMainViewPluginViewDescriptor = useMemo<WorkbenchViewDescriptor | null>(
    () =>
      mainViewPluginViewAvailability.find(
        ({ descriptor, availability }) =>

          descriptor.id !== mapContributionId &&
          !isContributionHidden(descriptor.id) &&
          (availability.available ||
            availability.reason?.startsWith('missing_capability:') === true),
      )?.descriptor ?? null,
    [isContributionHidden, mainViewPluginViewAvailability, mapContributionId],
  );
  const pluginPanelDescriptors = useMemo(
    () => pluginPanelDescriptorsFromRuntimeIndex(workbenchPluginRuntimeIndex),
    [workbenchPluginRuntimeIndex],
  );
  const pluginCommandDescriptors = useMemo(
    () => pluginCommandDescriptorsFromRuntimeIndex(workbenchPluginRuntimeIndex),
    [workbenchPluginRuntimeIndex],
  );

  // Resolve plugins only through the trusted-module path.
  const modalOrPeekPluginPanelDescriptors = useMemo(
    () =>
      pluginPanelDescriptors.filter((descriptor) =>
        descriptor.placements.some(
          (placement) => placement.host === 'modalOrPeek' && placement.mode === 'peek',
        ),
      ),
    [pluginPanelDescriptors],
  );

  const {
    dismissPluginPeek,
    openPluginPeekDescriptor,
    openPluginPeekForPlugin,
    pluginPeekState,
  } = usePluginPeekController(modalOrPeekPluginPanelDescriptors);

  const workbenchCommandEntries = useMemo<WorkbenchCommandEntry[]>(() => {
    const entries: WorkbenchCommandEntry[] = [
      firstPartyCommandEntry(
        { type: 'sources.open' },
        commandContext,
        OPEN_SOURCES_COMMAND_DESCRIPTOR,
      ),
      firstPartyCommandEntry(
        { type: 'settings.open' },
        commandContext,
        OPEN_SETTINGS_COMMAND_DESCRIPTOR,
      ),
      firstPartyCommandEntry(
        { type: 'ocrCompare.open' },
        commandContext,
        OCR_COMPARE_COMMAND_DESCRIPTOR,
      ),
      firstPartyCommandEntry(
        { type: 'transcribeCompare.open' },
        commandContext,
        TRANSCRIBE_COMPARE_COMMAND_DESCRIPTOR,
      ),
      firstPartyCommandEntry(
        { type: 'translateCompare.open' },
        commandContext,
        TRANSLATE_COMPARE_COMMAND_DESCRIPTOR,
      ),
      firstPartyCommandEntry(
        { type: 'topicCompare.open' },
        commandContext,
        TOPIC_COMPARE_COMMAND_DESCRIPTOR,
      ),
      ...pluginCommandDescriptors.map((descriptor) =>
        pluginCommandEntry({
          descriptor,
          sheet: sheet ?? null,
          hostContext: workbenchHostContext,
          onRan: (ranDescriptor) => recordCommandAction(`Ran ${ranDescriptor.title}`),
          openPeek: openPluginPeekForPlugin(descriptor.ownerPluginId),
        }),
      ),
    ];
    return entries.sort((left, right) => {
      const leftOrder = left.descriptor.placements[0]?.order ?? 0;
      const rightOrder = right.descriptor.placements[0]?.order ?? 0;
      return leftOrder - rightOrder || left.descriptor.id.localeCompare(right.descriptor.id);
    });
  }, [
    commandContext,
    openPluginPeekForPlugin,
    pluginCommandDescriptors,
    recordCommandAction,
    sheet,
    workbenchHostContext,
  ]);
  const rightInspectorPluginPanelDescriptors = useMemo(
    () =>
      pluginPanelDescriptors.filter((descriptor) =>
        descriptor.placements.some((placement) => placement.host === 'rightInspector'),
      ),
    [pluginPanelDescriptors],
  );
  const bottomDockPluginPanelDescriptors = useMemo(
    () =>
      pluginPanelDescriptors.filter((descriptor) =>
        descriptor.placements.some((placement) => placement.host === 'bottomDock'),
      ),
    [pluginPanelDescriptors],
  );
  const leftSidebarPluginPanelDescriptors = useMemo(
    () =>
      pluginPanelDescriptors.filter((descriptor) =>
        descriptor.placements.some((placement) => placement.host === 'leftSidebar'),
      ),
    [pluginPanelDescriptors],
  );
  const rowDetailPluginPanelDescriptors = useMemo(
    () =>
      pluginPanelDescriptors.filter((descriptor) =>
        descriptor.placements.some((placement) => placement.host === 'rowDetail'),
      ),
    [pluginPanelDescriptors],
  );
  const columnDetailPluginPanelDescriptors = useMemo(
    () =>
      pluginPanelDescriptors.filter((descriptor) =>
        descriptor.placements.some(
          (placement) =>
            placement.host === 'columnDetail' || placement.host === 'columnInspector',
        ),
      ),
    [pluginPanelDescriptors],
  );
  const entityDetailPluginPanelDescriptors = useMemo(
    () =>
      pluginPanelDescriptors.filter((descriptor) =>
        descriptor.placements.some((placement) => placement.host === 'entityDetail'),
      ),
    [pluginPanelDescriptors],
  );
  const sourceDetailPluginPanelDescriptors = useMemo(
    () =>
      pluginPanelDescriptors.filter((descriptor) =>
        descriptor.placements.some((placement) => placement.host === 'sourceDetail'),
      ),
    [pluginPanelDescriptors],
  );

  const renderPluginDetailTab = useCallback(
    (
      contribution: WorkbenchResolvedLayoutContribution,
      subject: PluginDetailSubject,
    ) => {
      if (contribution.runtimeSource !== 'runtimeIndex' || !sheet) return null;
      const descriptor = pluginPanelDescriptors.find(
        (candidate) => candidate.id === contribution.contributionId,
      );
      if (!descriptor) return null;
      return (
        <PluginDetailHost
          descriptor={descriptor}
          sheet={sheet}
          hostContext={workbenchHostContext}
          host={contribution.host}
          subject={subject}
        />
      );
    },
    [pluginPanelDescriptors, sheet, workbenchHostContext],
  );

  const disabledContributionReasons = useMemo<Record<string, string>>(() => {
    const reasons: Record<string, string> = {};
    if (mapContributionId && mapAvailabilityStatus === 'disabled' && !mapHidden) {
      reasons[mapContributionId] = mapAvailabilityReason;
    }
    if (
      !imageGalleryAvailability.available &&
      imageGalleryAvailability.reason.startsWith('missing_capability:')
    ) {
      reasons[IMAGE_GALLERY_VIEW_DESCRIPTOR.id] = imageGalleryAvailability.reason;
    }
    for (const { descriptor, availability } of mainViewPluginViewAvailability) {

      if (descriptor.id in reasons) continue;
      if (!availability.available) {
        reasons[descriptor.id] = availability.reason;
      }
    }
    for (const descriptor of FIRST_PARTY_WORKBENCH_CONTRIBUTION_DESCRIPTORS) {
      if (descriptor.id in reasons) continue;
      const unmet = firstMissingDataRequirement(
        descriptor.dataRequirements,
        workbenchDataRequirementContext,
      );
      if (unmet) {
        reasons[descriptor.id] = dataRequirementReason(unmet);
      }
    }
    return reasons;
  }, [
    mapContributionId,
    mapAvailabilityStatus,
    mapHidden,
    mapAvailabilityReason,
    imageGalleryAvailability,
    mainViewPluginViewAvailability,
    workbenchDataRequirementContext,
  ]);

  const firstPartyContributionDescriptors = useMemo<WorkbenchStampedContributionDescriptor[]>(
    () =>
      FIRST_PARTY_WORKBENCH_CONTRIBUTION_DESCRIPTORS.map((descriptor) => ({
        descriptor,
        runtimeSource: 'firstParty',
      })),
    [],
  );

  const resolvedWorkbenchLayout = useMemo(() => resolveWorkbenchLayout({
    hiddenContributionIds,
    missingContributionIds: EMPTY_MISSING_CONTRIBUTION_IDS,
    disabledContributionReasons,
    runtimeIndexContributionIdsByRegion: {
      mainView: mainViewPluginViewDescriptors.map((descriptor) => descriptor.id),
      rightInspector: rightInspectorPluginPanelDescriptors.map(
        (descriptor) => descriptor.id,
      ),
      bottomDock: bottomDockPluginPanelDescriptors.map((descriptor) => descriptor.id),
      leftSidebar: leftSidebarPluginPanelDescriptors.map((descriptor) => descriptor.id),
      rowDetail: rowDetailPluginPanelDescriptors.map((descriptor) => descriptor.id),
      columnDetail: columnDetailPluginPanelDescriptors.map((descriptor) => descriptor.id),
      columnInspector: columnDetailPluginPanelDescriptors.map(
        (descriptor) => descriptor.id,
      ),
      entityDetail: entityDetailPluginPanelDescriptors.map((descriptor) => descriptor.id),
      sourceDetail: sourceDetailPluginPanelDescriptors.map((descriptor) => descriptor.id),
      modalOrPeek: modalOrPeekPluginPanelDescriptors.map((descriptor) => descriptor.id),
      activityRail: activityRailPluginDescriptors.map((descriptor) => descriptor.id),
    },
    contributionDescriptorsByRegion: {
      mainView: [...firstPartyContributionDescriptors, ...stampRuntimeIndex(mainViewPluginViewDescriptors)],
      rightInspector: [
        ...firstPartyContributionDescriptors,
        ...stampRuntimeIndex(rightInspectorPluginPanelDescriptors),
      ],
      bottomDock: [
        ...firstPartyContributionDescriptors,
        ...stampRuntimeIndex(bottomDockPluginPanelDescriptors),
      ],
      leftSidebar: [
        ...firstPartyContributionDescriptors,
        ...stampRuntimeIndex(leftSidebarPluginPanelDescriptors),
      ],
      rowDetail: [
        ...firstPartyContributionDescriptors,
        ...stampRuntimeIndex(rowDetailPluginPanelDescriptors),
      ],
      columnDetail: [
        ...firstPartyContributionDescriptors,
        ...stampRuntimeIndex(columnDetailPluginPanelDescriptors),
      ],
      columnInspector: [
        ...firstPartyContributionDescriptors,
        ...stampRuntimeIndex(columnDetailPluginPanelDescriptors),
      ],
      entityDetail: [
        ...firstPartyContributionDescriptors,
        ...stampRuntimeIndex(entityDetailPluginPanelDescriptors),
      ],
      sourceDetail: [
        ...firstPartyContributionDescriptors,
        ...stampRuntimeIndex(sourceDetailPluginPanelDescriptors),
      ],
      modalOrPeek: [
        ...firstPartyContributionDescriptors,
        ...stampRuntimeIndex(modalOrPeekPluginPanelDescriptors),
      ],
      activityRail: [
        ...firstPartyContributionDescriptors,
        ...stampRuntimeIndex(activityRailPluginDescriptors),
      ],

      rowInspector: firstPartyContributionDescriptors,
      commandPalette: firstPartyContributionDescriptors,
    },
  }), [
    hiddenContributionIds,
    disabledContributionReasons,
    firstPartyContributionDescriptors,
    mainViewPluginViewDescriptors,
    rightInspectorPluginPanelDescriptors,
    bottomDockPluginPanelDescriptors,
    leftSidebarPluginPanelDescriptors,
    rowDetailPluginPanelDescriptors,
    columnDetailPluginPanelDescriptors,
    entityDetailPluginPanelDescriptors,
    sourceDetailPluginPanelDescriptors,
    modalOrPeekPluginPanelDescriptors,
    activityRailPluginDescriptors,
  ]);

  const resolvedRegion = useCallback(
    (regionId: WorkbenchHostId) => {
      const region = resolvedWorkbenchLayout.find((item) => item.regionId === regionId);
      if (!region) {
        return { regionId, contributions: [] };
      }
      return region;
    },
    [resolvedWorkbenchLayout],
  );
  const workbenchVisibilityTargets = useMemo(() => {
    const targets: WorkbenchVisibilityTarget[] = [];
    for (const region of resolvedWorkbenchLayout) {
      for (const contribution of region.contributions) {
        const target = workbenchVisibilityTargetFromContribution(contribution);
        if (target !== null) targets.push(target);
      }
    }
    return targets;
  }, [resolvedWorkbenchLayout]);

  const revealPluginLauncher = useCallback(
    (contributionId: string) => {
      const descriptor = activityRailPluginDescriptors.find(
        (candidate) => candidate.id === contributionId,
      );
      if (!descriptor) return;
      const primary = descriptor.placements.find(
        (placement) => placement.host !== 'activityRail',
      );
      if (!primary) return;
      // Unhide a target before focusing it.

      if (isContributionHidden(contributionId)) {
        revealContribution(contributionId);
      }
      switch (primary.host) {
        case 'bottomDock':

          setActiveBottomDockTab(normalizePlacement(descriptor, primary).placementId);
          return;
        case 'leftSidebar':

          openDiscover(contributionId);
          return;
        case 'mainView':
        default:

          return;
      }
    },
    [
      activityRailPluginDescriptors,
      isContributionHidden,
      openDiscover,
      revealContribution,
      setActiveBottomDockTab,
    ],
  );

  const pluginLauncherCommands = useMemo(
    () =>
      activityRailPluginDescriptors.map((descriptor) => ({
        contributionId: descriptor.id,
        label: descriptor.shortTitle ?? descriptor.title,
      })),
    [activityRailPluginDescriptors],
  );

  const rowDrawerOpen = rowDrawer !== null;

  const [replayReview, setReplayReview] = useState<{
    columnId: string;
    columnName: string;
    entries: ReplayReviewEntry[];
    activeRowId: string | null;
  } | null>(null);
  const [replayBusy, setReplayBusy] = useState(false);

  const loadReplayPending = useCallback(
    async (columnId: string): Promise<ReplayReviewEntry[]> => {
      if (!sheet) return [];
      const page = await projectApi.getSheetData(sheet.id, 0, REPLAY_REVIEW_PAGE_SIZE);
      const entries: ReplayReviewEntry[] = [];
      for (const row of page.rows) {
        const pending = row.replayPending?.[columnId];
        if (pending) {
          entries.push({
            rowId: row.id,
            edit: row.cells[columnId] ?? null,
            fresh: pending.freshValue,
            hash: pending.generatedValueHash,
            runId: pending.runId,
          });
        }
      }
      return entries;
    },
    [sheet],
  );

  const startReplayReview = useCallback(
    async (columnId: string, columnName: string) => {
      try {
        const entries = await loadReplayPending(columnId);
        if (entries.length === 0) return;
        const first = firstPendingRowId(entries.map((entry) => Number(entry.rowId)));
        setReplayReview({
          columnId,
          columnName,
          entries,
          activeRowId: first != null ? String(first) : entries[0].rowId,
        });
      } catch (e: unknown) {
        showError(e instanceof Error ? e.message : String(e));
      }
    },
    [loadReplayPending, showError],
  );

  const refreshReplayAfterAction = useCallback(
    async (columnId: string, columnName: string, actedRowId: string) => {
      const orderedBefore = replayReview?.entries.map((entry) => Number(entry.rowId)) ?? [];
      const nextNum = advanceAfterAction(orderedBefore, Number(actedRowId));
      projectData.invalidate();
      void refreshHistory();
      // Refresh metadata before surfacing replay-pending counts.

      void refreshSheets();
      const entries = await loadReplayPending(columnId);
      if (entries.length === 0) {
        setReplayReview(null);
        return;
      }
      const stillPending = new Set(entries.map((entry) => entry.rowId));
      const nextId =
        nextNum != null && stillPending.has(String(nextNum))
          ? String(nextNum)
          : entries[0].rowId;
      setReplayReview({ columnId, columnName, entries, activeRowId: nextId });
    },
    [replayReview, loadReplayPending, projectData, refreshHistory, refreshSheets],
  );

  const acceptActiveReplay = useCallback(async () => {
    if (!sheet || !replayReview?.activeRowId) return;
    const { columnId, columnName, activeRowId } = replayReview;
    setReplayBusy(true);
    try {
      const entry = replayReview.entries.find((candidate) => candidate.rowId === activeRowId);
      if (!entry) return;
      await projectApi.acceptReplayValue(
        sheet.id, columnId, activeRowId, entry.hash, entry.runId,
      );
      await refreshReplayAfterAction(columnId, columnName, activeRowId);
    } catch (e: unknown) {
      showError(e instanceof Error ? e.message : String(e));
    } finally {
      setReplayBusy(false);
    }
  }, [sheet, replayReview, refreshReplayAfterAction, showError]);

  const keepActiveReplay = useCallback(async () => {
    if (!sheet || !replayReview?.activeRowId) return;
    const { columnId, columnName, activeRowId, entries } = replayReview;
    const entry = entries.find((candidate) => candidate.rowId === activeRowId);
    if (!entry) return;
    setReplayBusy(true);
    try {
      await projectApi.dismissReplayPending(sheet.id, columnId, activeRowId, entry.hash, entry.runId);
      await refreshReplayAfterAction(columnId, columnName, activeRowId);
    } catch (e: unknown) {
      showError(e instanceof Error ? e.message : String(e));
    } finally {
      setReplayBusy(false);
    }
  }, [sheet, replayReview, refreshReplayAfterAction, showError]);

  const acceptAllReplay = useCallback(
    async (columnId: string) => {
      if (!sheet) return;
      setReplayBusy(true);
      try {
        await projectApi.acceptReplayValuesInColumn(sheet.id, columnId);
        projectData.invalidate();
        void refreshHistory();
        void refreshSheets();
        setReplayReview(null);
      } catch (e: unknown) {
        showError(e instanceof Error ? e.message : String(e));
      } finally {
        setReplayBusy(false);
      }
    },
    [sheet, projectData, refreshHistory, refreshSheets, showError],
  );

  const dismissColumnReplay = useCallback(
    async (columnId: string) => {
      if (!sheet) return;
      try {
        const entries = await loadReplayPending(columnId);
        await Promise.all(
          entries.map((entry) =>
            projectApi.dismissReplayPending(
              sheet.id,
              columnId,
              entry.rowId,
              entry.hash,
              entry.runId,
            ),
          ),
        );
        projectData.invalidate();
        void refreshHistory();
        void refreshSheets();
        setReplayReview(null);
      } catch (e: unknown) {
        showError(e instanceof Error ? e.message : String(e));
      }
    },
    [sheet, loadReplayPending, projectData, refreshHistory, refreshSheets, showError],
  );

  const replayColumnAnnotations = useMemo<ColumnAnnotation[]>(() => {
    if (!sheet) return [];
    const annotations: ColumnAnnotation[] = [];
    for (const column of sheet.columns) {
      const count = column.replayPendingCount;
      const latestRunId = column.latestRunId;
      if (!count || latestRunId == null) continue;
      annotations.push(
        buildReplayPendingAnnotation({
          columnId: column.id,
          runId: Number(latestRunId),
          message: (
            <span className="replay-pending-chip">
              <span>{replayPendingChipLabel(count)}</span>
              <button
                type="button"
                className="replay-pending-review"
                data-testid="replay-review-button"
                onClick={() => void startReplayReview(column.id, column.name)}
              >
                Review
              </button>
              <button
                type="button"
                className="replay-pending-accept-all"
                data-testid="replay-accept-all-button"
                onClick={() => void acceptAllReplay(column.id)}
              >
                Accept all
              </button>
            </span>
          ),
          onDismiss: () => void dismissColumnReplay(column.id),
        }),
      );
    }
    return annotations;
  }, [sheet, startReplayReview, acceptAllReplay, dismissColumnReplay]);

  const replayReviewEntry = replayReview?.activeRowId
    ? replayReview.entries.find((entry) => entry.rowId === replayReview.activeRowId)
    : undefined;
  const gridContribution = useMemo(() => activePreviewView?.result?.kind === 'table'
    ? <PreviewTable result={activePreviewView.result} /> : sheet ? (
    <GridWorkbenchViewFrame>
      <Suspense fallback={<PanelLoading className="grid-host" label="Loading grid…" />}>
        <LazySheetGrid
          sheet={sheet}
          dataVersion={dataVersion}
          liveRun={run}
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
      {replayReview && replayReviewEntry && (
        <ReplayAcceptPopover
          columnName={replayReview.columnName}
          yourEdit={replayReviewEntry.edit}
          newerGenerated={replayReviewEntry.fresh}
          busy={replayBusy}
          onAccept={() => void acceptActiveReplay()}
          onKeep={() => void keepActiveReplay()}
          onClose={() => setReplayReview(null)}
        />
      )}
    </GridWorkbenchViewFrame>
  ) : null, [
    sheet,
    dataVersion,
    run,
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
    replayReview,
    replayReviewEntry,
    replayBusy,
    acceptActiveReplay,
    keepActiveReplay,
  ]);

  // Keep clearChildFilter in parity coverage.

  const toggleProvenanceWithDrawers = useCallback(
    () => toggleProvenanceWithDrawersTransition({ detail, chrome }),
    [detail, chrome],
  );
  const clearChildFilter = useCallback(() => detail.setChildFilter(null), [detail]);

  const mainViewModel = useMemo(
    () => ({
      routePanel,
      sheet,
      sourceHealthHidden,
      evidenceMainViewHidden,
      evidenceViewerState,
      graphSplitShowing,
      mapSplitShowing,
      mapContributionId,
      resolvedRegion,
      ocrCompareOpen,
      ocrCompareActive,
      ocrCompareTarget,
      openOcrCompareTab,
      transcribeCompareOpen,
      transcribeCompareActive,
      ocrCompareSession,
      ocrCompareCloseWarn,
      setOcrCompareSession,
      setOcrCompareCloseWarn,
      closeOcrCompareTab,
      transcribeCompareSession,
      transcribeCompareCloseWarn,
      setTranscribeCompareSession,
      setTranscribeCompareCloseWarn,
      closeTranscribeCompareTab,
      translateCompareOpen,
      translateCompareActive,
      translateCompareSession,
      translateCompareCloseWarn,
      setTranslateCompareSession,
      setTranslateCompareCloseWarn,
      closeTranslateCompareTab,
      topicCompareOpen,
      topicCompareActive,
      topicCompareSession,
      topicCompareCloseWarn,
      setTopicCompareSession,
      setTopicCompareCloseWarn,
      closeTopicCompareTab,
      activePromotedView,
      closeRoutePanel,
      refreshHistory,
      refreshSheets,
      selectSheet,
      invalidateProjectData: projectData.invalidate,
      applyInlineFilter,
      appendRow,
      requestDeleteRows,
      selectedRowIdsForSheet,
      toggleWrapText,
      activeWorkView,
      workViewSegments,
      workViewDisabledReasons,
      setWorkView,
      visibleRowCount,
      activeHiddenColumns,
      changeRowHeight,
      overflowMenuOpen,
      setOverflowMenuOpen: chrome.setOverflowMenuOpen,
      provenanceOpen,
      toggleProvenanceWithDrawers,
      revealAllHiddenColumns,
      ensureViewsLoaded,
      canCreateSavedView,
      openSavedViews,
      openNewSavedView,
      startCreatingSavedView,
      applyGridSort,
      applySavedView,
      clearGridFilter,
      clearGridSort,
      deleteView,
      startSavedViewDelete,
      startSavedViewDefinitionUpdate,
      replaceSavedViewDefinition,
      savedViewsEditor,
      savedViewsSaving,
      savedViewsConfirmation,
      savedViewsConfirmationError,
      savedViewsLastPublication,
      consumeSavedViewsPublication: savedViews.consumePublication,
      cancelSavedViewsConfirmation: savedViews.cancelConfirmation,
      effectiveSortColumn,
      saveCurrentView,
      cancelSavedViewEditor: savedViews.cancelEditor,
      setViewName: savedViews.setViewName,
      startEditingView: savedViews.startEditingView,
      renameSavedView,
      viewName,
      views,
      activeSavedViewId,
      addWatchForCurrentView,
      openNotificationSettings,
      activeChildFilter,
      activeLensView,
      activePreviewView,
      clearChildFilter,
      closePreviewView,
      runPreviewForReal,
      exitLensView,
      lensOpenError,
      activeMainViewPluginViewDescriptor,
      applyGridSortForColumn,
      closeEvidenceViewer,
      closeSplit,
      documentView,
      documentViewShowing,
      annotatedTextColumnIds,
      documentAnnotationPreferences,
      setSheetAnnotationToggles,
      answersViewShowing,
      answersCitedColumns,
      gridContribution,
      gridOnly,
      hiddenContributionIds,
      imageGalleryAvailability,
      mapColumn,
      mapViewDescriptor,
      openRowById,
      openRowRef,
      project,
      promoteCurrentView,
      renderPluginDetailTab,
      selectAnswersRow,
      selectDocumentRow,
      selectedRowCountForSheet: selectedRowIdsForSheet.length,
      setDocumentView,
      visibleOrderedColumns,
      workbenchHostContext,
      graphNeighborhoodHidden,
      actActionTemplates,
      activeFrozenColumnCount,
      changeFrozenColumnCount,
      closeHeaderMenu,
      headerMenuActiveFilter,
      headerMenuActiveSort,
      headerMenuAdjacentHidden,
      hideColumn,
      insertColumnBeside,
      setRowTitleColumn,
      mapAvailabilityReason,
      mapAvailabilityStatus,
      mapAvailable,
      openHeaderMenuColumnSettings,
      openHeaderMenuSaveView,
      openMapPanel,
      revealColumns,
      runActionFromSurface,
      startRun,
      visibleHeaderMenu,
      onImported,
      showError,
    }),
    [
      routePanel,
      sheet,
      annotatedTextColumnIds,
      documentAnnotationPreferences,
      setSheetAnnotationToggles,
      sourceHealthHidden,
      evidenceMainViewHidden,
      evidenceViewerState,
      graphSplitShowing,
      mapSplitShowing,
      mapContributionId,
      resolvedRegion,
      ocrCompareOpen,
      ocrCompareActive,
      ocrCompareTarget,
      openOcrCompareTab,
      transcribeCompareOpen,
      transcribeCompareActive,
      ocrCompareSession,
      ocrCompareCloseWarn,
      setOcrCompareSession,
      setOcrCompareCloseWarn,
      closeOcrCompareTab,
      transcribeCompareSession,
      transcribeCompareCloseWarn,
      setTranscribeCompareSession,
      setTranscribeCompareCloseWarn,
      closeTranscribeCompareTab,
      translateCompareOpen,
      translateCompareActive,
      translateCompareSession,
      translateCompareCloseWarn,
      setTranslateCompareSession,
      setTranslateCompareCloseWarn,
      closeTranslateCompareTab,
      topicCompareOpen,
      topicCompareActive,
      topicCompareSession,
      topicCompareCloseWarn,
      setTopicCompareSession,
      setTopicCompareCloseWarn,
      closeTopicCompareTab,
      activePromotedView,
      closeRoutePanel,
      refreshHistory,
      refreshSheets,
      selectSheet,
      projectData,
      applyInlineFilter,
      appendRow,
      requestDeleteRows,
      selectedRowIdsForSheet,
      toggleWrapText,
      activeWorkView,
      workViewSegments,
      workViewDisabledReasons,
      setWorkView,
      visibleRowCount,
      activeHiddenColumns,
      changeRowHeight,
      overflowMenuOpen,
      chrome,
      provenanceOpen,
      toggleProvenanceWithDrawers,
      revealAllHiddenColumns,
      savedViews,
      ensureViewsLoaded,
      canCreateSavedView,
      openSavedViews,
      openNewSavedView,
      startCreatingSavedView,
      applyGridSort,
      applySavedView,
      clearGridFilter,
      clearGridSort,
      deleteView,
      startSavedViewDelete,
      startSavedViewDefinitionUpdate,
      replaceSavedViewDefinition,
      savedViewsEditor,
      savedViewsSaving,
      savedViewsConfirmation,
      savedViewsConfirmationError,
      savedViewsLastPublication,
      effectiveSortColumn,
      saveCurrentView,
      renameSavedView,
      viewName,
      views,
      activeSavedViewId,
      addWatchForCurrentView,
      openNotificationSettings,
      activeChildFilter,
      activeLensView,
      activePreviewView,
      clearChildFilter,
      closePreviewView,
      runPreviewForReal,
      exitLensView,
      lensOpenError,
      activeMainViewPluginViewDescriptor,
      applyGridSortForColumn,
      closeEvidenceViewer,
      closeSplit,
      documentView,
      documentViewShowing,
      answersViewShowing,
      answersCitedColumns,
      gridContribution,
      gridOnly,
      hiddenContributionIds,
      imageGalleryAvailability,
      mapColumn,
      mapViewDescriptor,
      openRowById,
      openRowRef,
      project,
      promoteCurrentView,
      renderPluginDetailTab,
      selectAnswersRow,
      selectDocumentRow,
      setDocumentView,
      visibleOrderedColumns,
      workbenchHostContext,
      graphNeighborhoodHidden,
      actActionTemplates,
      activeFrozenColumnCount,
      changeFrozenColumnCount,
      closeHeaderMenu,
      headerMenuActiveFilter,
      headerMenuActiveSort,
      headerMenuAdjacentHidden,
      hideColumn,
      insertColumnBeside,
      setRowTitleColumn,
      mapAvailabilityReason,
      mapAvailabilityStatus,
      mapAvailable,
      openHeaderMenuColumnSettings,
      openHeaderMenuSaveView,
      openMapPanel,
      revealColumns,
      runActionFromSurface,
      startRun,
      visibleHeaderMenu,
      onImported,
      showError,
    ],
  );
  const overlayModel = useMemo(
    () => ({
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
      closeRowDrawer: detail.clearRowDrawer,
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
      openEvidenceViewer: openEvidenceViewerForWorkspace,
      openReview,
      project,
      openPluginPeekDescriptor,
      pluginPeekState,
      provenanceOpen,
      closeProvenanceOpen: chrome.closeProvenanceOpen,
      pluginLauncherCommands,
      renderPluginDetailTab,
      resolvedRegion,
      revealPluginLauncher,
      revealWorkbenchContribution,
      reviewCount,
      reviewOpen,
      reviewRunId,
      setProposalInspect: detail.setProposalInspect,
      sheet,
      commitReviewDecision: projectData.commitReviewDecision,
      workbenchCommandEntries,
      workbenchHostContext,
      workbenchVisibilityTargets,
    }),
    [
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
      detail,
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
      openEvidenceViewerForWorkspace,
      openReview,
      project,
      openPluginPeekDescriptor,
      pluginPeekState,
      provenanceOpen,
      chrome,
      pluginLauncherCommands,
      renderPluginDetailTab,
      resolvedRegion,
      revealPluginLauncher,
      revealWorkbenchContribution,
      reviewCount,
      reviewOpen,
      reviewRunId,
      sheet,
      projectData,
      workbenchCommandEntries,
      workbenchHostContext,
      workbenchVisibilityTargets,
    ],
  );

  return {

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
