import './WorkspaceSurface.css';
import '../grid/SheetGrid.css';
import { lazy, Suspense, useCallback, useMemo, type ReactNode } from 'react';
import { toWireJsonObject } from '../api/viewsLenses';
import { X } from 'lucide-react';
import { listProjectDocuments } from '../api/open';
import { PanelLoading } from '../components/PanelPrimitives';
import { useResizable } from '../components/useResizable';
import { ResizeSeam } from '../components/ResizeSeam';
import { nerReplayPlan } from '../workbench/nerReplayModel';
import { DocumentView } from '../workbench/DocumentView';
import { ExtractView } from '../workbench/extract/ExtractView';
import { isExtractionRunning } from '../workbench/extract/extractionRunning';
import { AnswersView } from '../workbench/AnswersView';
import { resolveTitleColumn } from '../workbench/rowTitle';
import { EvidenceWorkbenchViewFrame, GraphNeighborhoodWorkbenchViewFrame } from '../workbench/contributions';
import { IMAGE_GALLERY_VIEW_DESCRIPTOR } from '../workbench/descriptors';
import { PluginMainViewHost } from '../workbench/PluginMainViewHost';
import { GRAPH_NEIGHBORHOOD_CONTRIBUTION_ID } from './contributionIds';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { useActionCatalogHandle } from '../bind/useActionCatalogHandle';
import { useWorkViewHandle } from '../bind/useWorkViewHandle';
import { useDetailHandle } from '../bind/useDetailHandle';
import { useJobsHandle } from '../bind/useJobsHandle';
import { useSelector } from '../bind/useSelector';
import { useCurrentSheet, useMainViewModel } from './WorkspaceContext';
import { LazyEvidenceViewer } from './WorkspaceOverlays';

const LazyGenericGraphView = lazy(() => (
  import('../components/GenericGraphView').then(({ GenericGraphView }) => ({
    default: GenericGraphView,
  }))
));

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

export function WorkspacePrimarySurface() {
  const { projectApi } = useWorkspaceStores();
  const { sheets } = useCurrentSheet();
  const jobs = useJobsHandle();
  const extractionRunning = useSelector(jobs.store, isExtractionRunning);
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
    documentPriority,
    prioritizeDocumentRow,
    setDocumentPriorityLabel,
    extractViewShowing,
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
    openPdfSplitForRow,
    openRowRef,
    openDocumentSimilarity,
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
  const detail = useDetailHandle();
  const childFilter = useSelector(detail.store, (current) => current.childFilter);
  const activeChildFilter = childFilter?.sheetId === sheet?.id ? childFilter : null;
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
  const scopeRowIds = useSelector(gridView.store, (s) => s.applied.scopeRowIds ?? null);
  const activeGridSort = useSelector(gridView.store, (s) => s.applied.sort);
  const extractionFilter = useMemo(() => activeGridFilter ? toWireJsonObject(activeGridFilter) : null, [activeGridFilter]);
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
        columnIds: args.columnIds, valueMode: 'bounded',
        filter: activeGridFilter,
        scopeRowIds,
        sort: activeGridSort,
      }),
    [activeGridFilter, activeGridSort, sheetIdForGallery, scopeRowIds],
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

  if ((documentViewShowing || extractViewShowing) && documentView && sheet) {

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
    const orderKey = `${activeChildFilter?.parentRowId ?? ''}|${JSON.stringify(scopeRowIds)}|${JSON.stringify(activeGridFilter ?? null)}|${JSON.stringify(
      activeGridSort ?? null,
    )}`;
    const queryDocuments = (args: {
      sourceColumnId: string; titleColumnId: string | null; query: string;
      cursor?: string; anchorRowId?: string; limit: number;
    }) => listProjectDocuments(project.id, sheet.id, {
      sourceColumnId: args.sourceColumnId,
      titleColumnId: args.titleColumnId,
      parentRowId: activeChildFilter?.parentRowId ?? null,
      filter: activeGridFilter,
      sort: activeGridSort,
      scopeRowIds,
      query: args.query,
      cursor: args.cursor,
      anchorRowId: args.anchorRowId,
      limit: args.limit,
    });
    const hydrateDocumentRow = async (rowId: string, columnIds: string[]) => {
      const page = await projectApi.getSheetData(sheet.id, 0, 1, {
        rowIds: [Number(rowId)], columnIds, valueMode: 'bounded',
      });
      return page.rows[0] ?? null;
    };
    if (extractViewShowing) {
      return <ExtractView
        key={sheet.id}
        projectId={project.id}
        sheet={sheet}
        state={documentView}
        onChangeState={setDocumentView}
        onDocumentFocus={selectDocumentRow}
        queryDocuments={queryDocuments}
        hydrateRow={hydrateDocumentRow}
        orderKey={orderKey}
        titleColumnOrder={titleColumnOrder}
        refreshKey={sheets}
        extractionRunning={extractionRunning}
        filterScope={{ filter: extractionFilter, parent_row_id: activeChildFilter?.parentRowId ? Number(activeChildFilter.parentRowId) : null, scope_row_ids: scopeRowIds }}
        onExtract={({ template, source, repeat_group_id, sheet_name, layout_id, extraction_scope }) => {
          const usedNames = new Set(sheets.map((entry) => entry.name));
          let uniqueName = sheet_name;
          for (let suffix = 2; usedNames.has(uniqueName); suffix++) uniqueName = `${sheet_name} (${suffix})`;
          startRun({ action_id: 'media.extract_document',
            scope: { kind: 'sheet_rows', sheet_id: Number(sheet.id) },
            params: { source, template, repeat_group_id, layout_id, extraction_scope },
            output_names: {}, sheet_name: uniqueName, idempotency_key: `extract-document:${crypto.randomUUID()}` });
        }}
      />;
    }
    return (
      <DocumentView
        projectId={project.id}
        sheet={sheet}
        state={documentView}
        onChangeState={setDocumentView}
        onDocumentFocus={selectDocumentRow}
        onOpenDetail={openRowById}
        onSplitPdf={openPdfSplitForRow}
        onFindSimilar={openDocumentSimilarity}
        queryDocuments={queryDocuments}
        hydrateRow={hydrateDocumentRow}
        orderKey={orderKey}
        listSortDir={listSortDir}
        titleColumnOrder={titleColumnOrder}
        annotatedTextColumnIds={annotatedTextColumnIds}
        disabledToggleKeys={documentAnnotationPreferences[sheet.id] ?? NO_DISABLED_TOGGLE_KEYS}
        onSetDisabledToggleKeys={(next) => setSheetAnnotationToggles(sheet.id, next)}
        onReplayAnnotationLayer={replayAnnotationLayer}
        priorityReadingRowId={documentPriority?.sheetId === sheet.id ? documentPriority.rowId : null}
        onRequestDocumentPriority={prioritizeDocumentRow}
        onPriorityLabel={setDocumentPriorityLabel}
        onListSort={(columnName, dir) => {
          if (dir === null) clearGridSort();
          else applyGridSortForColumn(columnName, dir);
        }}
      />
    );
  }

  if (answersViewShowing && sheet) {

    const titleColumnOrder = visibleOrderedColumns.map((column) => column.name);
    const orderKey = `${JSON.stringify(scopeRowIds)}|${JSON.stringify(activeGridFilter ?? null)}|${JSON.stringify(
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
              key={`${String(evidenceViewerState.linkId)}:${evidenceViewerState.scopeSpanId ?? ''}`}
              evidenceLinkId={evidenceViewerState.linkId}
              initialPayload={evidenceViewerState.initialPayload}
              initialPage={evidenceViewerState.initialPage}
              scopeSpanId={evidenceViewerState.scopeSpanId}
              scopeSpanIds={evidenceViewerState.scopeSpanIds}
              highlight={evidenceViewerState.highlight}
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

export function WorkspacePromotedView() {
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
  const scopeRowIds = useSelector(gridView.store, (s) => s.applied.scopeRowIds ?? null);
  const activeGridSort = useSelector(gridView.store, (s) => s.applied.sort);
  const sheetId = sheet?.id ?? '';
  const queryRows = useCallback(
    (args: { columnIds?: string[]; offset: number; limit: number }) =>
      projectApi.getSheetData(sheetId, args.offset, args.limit, {
        columnIds: args.columnIds, valueMode: 'bounded',
        filter: activeGridFilter,
        scopeRowIds,
        sort: activeGridSort,
      }),
    [activeGridFilter, activeGridSort, sheetId, scopeRowIds],
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
