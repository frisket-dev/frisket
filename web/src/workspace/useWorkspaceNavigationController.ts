import {
  useCallback, useEffect, useMemo, useRef, useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import type {
  ActionTemplate, ColumnDef, PreviewCellDetail, ProjectInfo, Row, SheetMeta,
} from '../api/open';
import type { ActionLaunchPrefill } from '../actions/actionFormInitial';
import type { ProjectSearchAnchorWire, ProjectSearchHitWire } from '../api/projectSearch';
import { promoteDocumentPriority } from '../api/documentPriority';
import { useActSurfaceHandle } from '../bind/useActSurfaceHandle';
import { useChromeHandle } from '../bind/useChromeHandle';
import { useDetailHandle } from '../bind/useDetailHandle';
import { useRouteHandle } from '../bind/useRouteHandle';
import { useRouteProjection } from '../bind/useRouteProjection';
import { useRouteRowFetch } from '../bind/useRouteRowFetch';
import { useRouteSyncController } from '../bind/useRouteSyncController';
import { useSelectionHandle } from '../bind/useSelectionHandle';
import { useSelector } from '../bind/useSelector';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import type { ChromeHandle } from '../core/route/projectRouteState';
import { routeStateToRoute, type RouteState } from '../core/route/RouteState';
import { requestGridCellReveal } from '../grid/gridCellReveal';
import { navigate, replaceRoute, type RoutePanel } from '../routes';
import type { EvidenceViewerFocus, EvidenceViewerHost } from '../state/chromeStore';
import {
  runActionFromSurfaceTransition, openRowPanelTransition, openSearchTextHitTransition,
  openColumnPanelTransition, openRowRefTransition, closeRoutePanelTransition,
} from '../state/workspaceTransitions';
import { actionsForColumn } from '../actions/model';
import { documentSimilarityLocation } from '../workbench/documentSimilarity';
import type { PaletteActionItem, PaletteGotoItem } from '../workbench/WorkbenchCommandPalette';
import type { PaletteSearchSeed } from '../workbench/usePaletteSearch';
import { isSupportedWorkbenchVisibilityTarget, type WorkbenchVisibilityTarget } from '../workbench/visibility';
import { EVIDENCE_CONTRIBUTION_ID, GRAPH_NEIGHBORHOOD_CONTRIBUTION_ID, SOURCE_HEALTH_CONTRIBUTION_ID } from './contributionIds';
import type { ChildFilter } from './workspaceState';
import type { useWorkspaceChromeState } from './useWorkspaceChromeState';
import type { useWorkspaceGridController } from './useWorkspaceGridController';
import type { useWorkbenchContributionVisibility } from './useWorkbenchContributionVisibility';

type ProjectSearchCellLocationWire = NonNullable<Extract<ProjectSearchAnchorWire, { kind: 'cell' }>['location']>;

/** The route sync controller is the sole history writer; reader focus never becomes action selection. */
export function useWorkspaceNavigationController({
  project, sheet, sheets, externalRoute, chromeState, visibility, grid,
  mapContributionId, actActionTemplates, importLiveRun, showError,
}: {
  project: ProjectInfo;
  sheet: SheetMeta | undefined;
  sheets: SheetMeta[];
  externalRoute: { routeSheetId?: string; routeActionKind?: string; routeReview?: boolean; routePanel?: RoutePanel };
  chromeState: Pick<ReturnType<typeof useWorkspaceChromeState>,
    'openEvidenceViewer' | 'setOpenSplit' | 'setDocumentView' | 'documentView' | 'openSplit' |
    'hideActionPanel' | 'recordCommandAction' | 'actionPanelOpen' | 'commandPaletteOpen'>;
  visibility: Pick<ReturnType<typeof useWorkbenchContributionVisibility>, 'isContributionHidden' | 'hideContribution' | 'revealContribution'>;
  grid: Pick<ReturnType<typeof useWorkspaceGridController>, 'activeChildFilter' | 'activeGridFilter' | 'activeGridSort' | 'scopeRowIds' | 'visibleHeaderMenu'>;
  mapContributionId: string | null;
  actActionTemplates: ActionTemplate[];
  importLiveRun: { runId: string; actionKind: string } | null;
  showError: (message: string) => void;
}) {
  const { projectApi } = useWorkspaceStores();
  const route = useRouteHandle();
  const detail = useDetailHandle();
  const selection = useSelectionHandle();
  const chrome = useChromeHandle();
  const actSurface = useActSurfaceHandle();
  const effectiveRoute = useSelector(route.store, (state) => state);
  const activeSheetId = effectiveRoute.sheetId;
  const routeActionKind = effectiveRoute.actionKind ?? undefined;
  const routePanel = effectiveRoute.panel ?? undefined;
  const routeReview = effectiveRoute.review;
  const rowDrawer = useSelector(detail.store, (state) => state.rowDrawer);
  const writeRoute = useCallback((next: RouteState) => route.navigate(next), [route]);
  const { routeSheetId, routeActionKind: externalActionKind, routeReview: externalReview, routePanel: externalPanel } = externalRoute;
  const {
    openEvidenceViewer, setOpenSplit, setDocumentView, documentView, openSplit,
    hideActionPanel, recordCommandAction, actionPanelOpen, commandPaletteOpen,
  } = chromeState;
  const { isContributionHidden, hideContribution, revealContribution } = visibility;
  const { activeChildFilter, activeGridFilter, activeGridSort, scopeRowIds, visibleHeaderMenu } = grid;
  const reviewOpen = routeReview === true;
  // A run-scoped queue reuses the same review route and overlay. The scope is
  // deliberately transient: it is an inspection affordance, not a saved view.
  const [reviewRunId, setReviewRunId] = useState<string | null>(null);

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

  const commandPaletteQuery = useSelector(chrome.store, (s) => s.commandPaletteQuery);
  const setCommandPaletteQuery = chrome.setCommandPaletteQuery;
  const [commandPaletteSearchSeed, setCommandPaletteSearchSeed] =
    useState<PaletteSearchSeed | null>(null);
  const closeCommandPaletteAndReset = useCallback(() => {
    setCommandPaletteSearchSeed(null);
    chrome.closeCommandPaletteAndReset();
  }, [chrome]);
  const openSearchPalette = useCallback((seed: PaletteSearchSeed | null = null) => {
    setCommandPaletteSearchSeed(seed);
    chrome.setCommandPaletteQuery('');
    chrome.openCommandPalette();
  }, [chrome]);

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
          openSearchPalette();
        }
      } else if (key === 'p' && event.shiftKey) {
        event.preventDefault();
        openSearchPalette();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [commandPaletteOpen, openSearchPalette, closeCommandPaletteAndReset]);

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

  const openCellSimilarity = useCallback((input: {
    sheetId: string;
    rowId: string;
    columnId: string;
    label: string;
    location?: ProjectSearchCellLocationWire;
    namedIndexId?: string;
  }) => {
    openSearchPalette({
      anchor: {
        kind: 'cell',
        sheet_id: Number(input.sheetId),
        row_id: Number(input.rowId),
        column_id: Number(input.columnId),
        exclude_exact: true,
        ...(input.location ? { location: input.location } : {}),
      },
      label: input.label,
      ...(input.namedIndexId ? { namedIndexId: input.namedIndexId } : {}),
    });
  }, [openSearchPalette]);

  const openDocumentSimilarity = useCallback((input: {
    sheetId: string;
    rowId: string;
    columnId: string;
    target: { kind: string; page?: number; timeSeconds?: number };
  }) => {
    const { label, location } = documentSimilarityLocation(input.target);
    openCellSimilarity({
      ...input,
      label,
      location,
    });
  }, [openCellSimilarity]);

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
      routeActionKind: externalActionKind ?? null,
      routeReview: !!externalReview,
      routePanel: externalPanel,
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
    activeGridScopeRowIds: scopeRowIds,
    rowDrawerId: rowDrawer?.loadedColumnIds ? null : rowDrawer?.id ?? null,
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
        .getSheetData(sheet.id, targetIndex, 1, { ...scope, valueMode: 'bounded' })
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
      projectApi,
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
    host: EvidenceViewerHost = 'modalOrPeek',
    focus?: EvidenceViewerFocus,
  ) => {
    if (host === 'mainView' && isContributionHidden(EVIDENCE_CONTRIBUTION_ID)) {
      return;
    }
    openEvidenceViewer(linkId, host, focus);
  }, [isContributionHidden, openEvidenceViewer]);

  const navigateToSearchHit = useCallback(
    (hit: ProjectSearchHitWire) => {
      const navigation = hit.navigation;
      if (navigation.kind === 'evidence') {
        openEvidenceViewerForWorkspace(navigation.evidence_link_id, 'modalOrPeek', {
          scopeSpanId: navigation.span_id,
          scopeSpanIds: navigation.span_ids,
        });
        return;
      }
      const sheetId = String(navigation.sheet_id);
      const rowId = String(navigation.row_id);
      if (navigation.kind === 'document') {
        setOpenSplit(null);
        setDocumentView({
          sheetId,
          sourceColumnId: String(navigation.column_id),
          titleColumnId: null,
          layout: 'continuous',
          fit: 'width',
          videoFit: 'full',
          textLayer: true,
          sync: false,
          activeRowId: rowId,
          readerLocationKey: crypto.randomUUID(),
          ...(navigation.page
            ? { readerPage: { rowId, page: navigation.page } }
            : {}),
          ...(navigation.time_seconds !== null && navigation.time_seconds !== undefined
            ? { readerTime: { rowId, seconds: navigation.time_seconds } }
            : {}),
        });
      } else if (navigation.column_id !== null && navigation.column_id !== undefined) {
        const columnId = String(navigation.column_id);
        const column = sheets.find((candidate) => candidate.id === sheetId)?.columns
          .find((candidate) => candidate.id === columnId);
        const dependency = hit.binding.dependencies.find((item) => (
          String(item.row_id) === rowId && String(item.column_id) === columnId
        ));
        // Search indexes JSON's decoded representation, not its stored encoding.
        // Only direct text with its authoritative revision can share these offsets.
        if (navigation.text_range && column?.type === 'text' && dependency) {
          openSearchTextHitTransition({ detail, selection }, writeRoute, project.id, { sheetId, rowId, columnId,
            range: { start: navigation.text_range.start, end: navigation.text_range.end,
              highlight: hit.location.precision === 'exact' },
            retrievalRevision: dependency.retrieval_revision, requestKey: crypto.randomUUID() });
          return;
        }
        requestGridCellReveal({
          projectId: project.id,
          sheetId,
          rowId,
          columnId: String(navigation.column_id),
          columnName: hit.column_name ?? String(navigation.column_id),
        });
      }
      selectSheet(sheetId);
      window.dispatchEvent(
        new CustomEvent('frisket:reveal-row', { detail: { sheetId, rowId } }),
      );
    },
    [
      detail,
      openEvidenceViewerForWorkspace,
      project.id,
      selectSheet,
      selection,
      setDocumentView,
      setOpenSplit,
      sheets,
      writeRoute,
    ],
  );

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

  const [documentPriority, setDocumentPriority] = useState<{
    sheetId: string;
    rowId: string;
    columnId: string;
    label: string;
    runId: string;
  } | null>(null);
  const documentPriorityRequest = useRef(0);
  const activeDocumentPriority = documentPriority?.runId === importLiveRun?.runId
    && importLiveRun?.actionKind === 'media.ocr' ? documentPriority : null;

  const prioritizeDocumentRow = useCallback(
    (rowId: string, columnId: string) => {
      if (!sheet) return;
      const numericRowId = Number(rowId);
      const numericColumnId = Number(columnId);
      if (!Number.isSafeInteger(numericRowId) || !Number.isSafeInteger(numericColumnId)) return;
      const request = ++documentPriorityRequest.current;
      void promoteDocumentPriority(project.id, {
        sheetId: Number(sheet.id),
        rowId: numericRowId,
        columnId: numericColumnId,
      }).then((result) => {
        if (request !== documentPriorityRequest.current || !result.promoted || result.runId === null) return;
        setDocumentPriority({
          sheetId: sheet.id,
          rowId,
          columnId,
          label: `File row ${rowId}`,
          runId: result.runId,
        });
      }).catch(() => undefined);
    },
    [project.id, sheet],
  );

  const openRowInDocumentView = useCallback(
    (rowId: string, columnId: string) => {
      if (!sheet) return;
      prioritizeDocumentRow(rowId, columnId);
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
    [sheet, prioritizeDocumentRow, documentView, selection, setDocumentView, setOpenSplit],
  );
  const openDocumentRef = useCallback(
    (sheetId: string, rowId: string, columnId: string) => {
      setOpenSplit(null);
      setDocumentView({
        sheetId,
        sourceColumnId: columnId,
        titleColumnId: null,
        layout: 'continuous',
        fit: 'width',
        videoFit: 'full',
        textLayer: true,
        sync: false,
        activeRowId: rowId,
        readerLocationKey: crypto.randomUUID(),
      });
      selection.clearRowSelection(sheetId);
      selectSheet(sheetId);
    },
    [selectSheet, selection, setDocumentView, setOpenSplit],
  );
  const setDocumentPriorityLabel = useCallback((rowId: string, label: string) => {
    setDocumentPriority((current) => current?.rowId === rowId ? { ...current, label } : current);
  }, []);

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
    // A launch can settle after sheet navigation. Read the current route rather
    // than letting its captured callback reopen the sheet that launched it.
    route.navigate({
      ...route.store.get(), actionKind: null, review: false, panel: null,
    });
  }, [hideActionPanel, route]);
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

  const mainView = useMemo(() => ({
    closeRoutePanel,
    selectSheet,
    openNotificationSettings,
    closeSplit,
    documentPriority: activeDocumentPriority,
    prioritizeDocumentRow,
    setDocumentPriorityLabel,
    openRowById,
    openRowRef,
    openDocumentSimilarity,
    openCellSimilarity,
    selectAnswersRow,
    selectDocumentRow,
    openMapPanel,
    runActionFromSurface,
  }), [
    closeRoutePanel, selectSheet, openNotificationSettings, closeSplit, activeDocumentPriority,
    prioritizeDocumentRow, setDocumentPriorityLabel, openRowById, openRowRef,
    openDocumentSimilarity, openCellSimilarity, selectAnswersRow, selectDocumentRow, openMapPanel,
    runActionFromSurface,
  ]);
  const overlays = useMemo(() => ({
    closeReview,
    closeRoutePanel,
    closeCommandPaletteAndReset,
    commandPaletteQuery,
    commandPaletteSearchSeed,
    setCommandPaletteQuery,
    commandPaletteBestMatchItems,
    commandPaletteActionItems,
    commandPaletteGotoItems,
    navigateToSearchHit,
    runActionFromSurface,
    selectSheet,
    documentPriority: activeDocumentPriority,
    hideWorkbenchContribution,
    openEvidenceViewer: openEvidenceViewerForWorkspace,
    openReview,
    revealWorkbenchContribution,
    reviewOpen,
    reviewRunId,
  }), [
    closeReview, closeRoutePanel, closeCommandPaletteAndReset, commandPaletteQuery,
    commandPaletteSearchSeed, setCommandPaletteQuery, commandPaletteBestMatchItems,
    commandPaletteActionItems, commandPaletteGotoItems, navigateToSearchHit, runActionFromSurface,
    selectSheet, activeDocumentPriority, hideWorkbenchContribution, openEvidenceViewerForWorkspace,
    openReview, revealWorkbenchContribution, reviewOpen, reviewRunId,
  ]);

  return useMemo(() => ({
    selectSheet, onMainViewTabKeyDown, workspaceRoute, runActionFromSurface,
    openSourcesFromCommandPalette, openSettingsFromCommandPalette, openRowPanel, openColumnPanel,
    walkDetailRow, openMapPanel, openGraphPanel, openMapSplit, openSourceHealthMainView,
    openEvidenceViewerForWorkspace, activeDocumentPriority, openRowInDocumentView, openDocumentRef, openRowRef,
    closeRoutePanel, closeActionRoute, closeActionDrawerOnLaunch, actionPanelVisible,
    openImportDialog, mainView, overlays,
  }), [
    selectSheet, onMainViewTabKeyDown, workspaceRoute, runActionFromSurface,
    openSourcesFromCommandPalette, openSettingsFromCommandPalette, openRowPanel, openColumnPanel,
    walkDetailRow, openMapPanel, openGraphPanel, openMapSplit, openSourceHealthMainView,
    openEvidenceViewerForWorkspace, activeDocumentPriority, openRowInDocumentView, openDocumentRef, openRowRef,
    closeRoutePanel, closeActionRoute, closeActionDrawerOnLaunch, actionPanelVisible,
    openImportDialog, mainView, overlays,
  ]);
}
