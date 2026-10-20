import { useCallback, useMemo, useState } from 'react';
import type { ColumnDef, SheetMeta } from '../api/open';
import type { OcrCompareTarget } from '../actions/ocrCompare';
import { useCompareViewHandle } from '../bind/useCompareViewHandle';
import { useSelectionHandle } from '../bind/useSelectionHandle';
import { useSelector } from '../bind/useSelector';
import { useWorkViewHandle } from '../bind/useWorkViewHandle';
import { selectWorkViewAvailability } from '../core/selectors/workView';
import { WORK_VIEW_KINDS, WORK_VIEW_TITLES } from '../core/selectors/views/registry';
import type { CompareTabSession } from '../state/compareViewStore';
import {
  setWorkViewResetTransition, setWorkViewGridTransition, setWorkViewDocumentTransition,
  setWorkViewExtractTransition, setWorkViewAnswersTransition, setWorkViewGalleryTransition,
  promoteCurrentViewTransition, openPromotedTabTransition, closePromotedTabTransition,
  openCompareTabTransition, selectSheetTabTransition,
} from '../state/workspaceTransitions';
import { documentMediaColumns } from '../workbench/documentMedia';
import { IMAGE_GALLERY_VIEW_DESCRIPTOR, type WorkbenchViewDescriptor } from '../workbench/descriptors';
import { resolvePluginViewAvailability } from '../workbench/pluginViewContext';
import {
  EVIDENCE_CONTRIBUTION_ID, GRAPH_NEIGHBORHOOD_CONTRIBUTION_ID, SOURCE_HEALTH_CONTRIBUTION_ID,
} from './contributionIds';
import type { PromotedView, WorkViewKind, useWorkspaceChromeState } from './useWorkspaceChromeState';
import type { useWorkbenchContributionVisibility } from './useWorkbenchContributionVisibility';

const NO_CITED_COLUMNS: readonly string[] = [];
const NO_CITED_COLUMNS_RESOLVED: readonly ColumnDef[] = [];

/** Owns view availability and transitions; comparison sessions remain in their domain handle. */
export function useWorkspaceViewController({
  sheet, chromeState, visibility, mapViewDescriptor, selectSheet, openMapPanel, openGraphPanel,
}: {
  sheet: SheetMeta | undefined;
  chromeState: Pick<ReturnType<typeof useWorkspaceChromeState>,
    'openSplit' | 'documentView' | 'promotedViews' | 'setOpenSplit' | 'setDocumentView' | 'setPromotedViews'>;
  visibility: Pick<ReturnType<typeof useWorkbenchContributionVisibility>, 'hiddenContributionIdSet' | 'isContributionHidden'>;
  mapViewDescriptor: WorkbenchViewDescriptor | null;
  selectSheet: (sheetId: string) => void;
  openMapPanel: (column: ColumnDef) => void;
  openGraphPanel: () => void;
}) {
  const workView = useWorkViewHandle();
  const compareView = useCompareViewHandle();
  const selection = useSelectionHandle();
  const { openSplit, documentView, promotedViews, setOpenSplit, setDocumentView, setPromotedViews } = chromeState;
  const { hiddenContributionIdSet, isContributionHidden } = visibility;
  const mapContributionId = mapViewDescriptor?.id ?? null;
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
  const extractViewSheetId = useSelector(workView.store, (s) => s.extractViewSheetId);

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
  const firstExtractSourceColumn =
    sheet?.columns.find((column) => column.type === 'file' || column.type === 'image') ?? null;

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
        firstExtractSourceColumn,
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
      firstExtractSourceColumn,
      imageGalleryAvailability.available,
      sheetIsEdgeShaped,
      citedColumnIds,
      annotatedTextColumnIds,
    ],
  );
  const documentAvailable = workViewAvailability.document.available;
  const extractAvailable = workViewAvailability.extract.available;
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

  const extractViewShowing =
    extractViewSheetId !== null &&
    extractViewSheetId === sheet?.id &&
    documentView?.sheetId === sheet?.id &&
    extractAvailable;

  const documentViewShowing =
    !extractViewShowing &&
    documentView !== null && documentView.sheetId === sheet?.id && documentAvailable;

  const answersViewShowing =
    answersViewSheetId !== null && answersViewSheetId === sheet?.id && answersAvailable;

  const activePromotedView = useMemo<PromotedView | null>(() => {
    if (!activePromotedKey || !sheet) return null;
    const view = promotedViews.find((candidate) => candidate.key === activePromotedKey);
    return view && view.sheetId === sheet.id ? view : null;
  }, [activePromotedKey, promotedViews, sheet]);

  const activeWorkView: WorkViewKind = extractViewShowing
    ? 'extract'
    : documentViewShowing
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
        case 'extract':
          setWorkViewExtractTransition(
            { setOpenSplit, setDocumentView },
            workView,
            sheetId,
            sheetId
              ? documentView && documentView.sheetId === sheetId
                ? { ...documentView, sync: false }
                : {
                    sheetId,
                    sourceColumnId: firstExtractSourceColumn
                      ? String(firstExtractSourceColumn.id)
                      : null,
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
      firstExtractSourceColumn,
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
    if (
      !sheet ||
      activeWorkView === 'grid' ||
      activeWorkView === 'answers' ||
      activeWorkView === 'extract'
    ) return;
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

  return useMemo(() => ({
    mapHidden, graphNeighborhoodHidden, evidenceMainViewHidden, sourceHealthHidden, mapColumn,
    imageGalleryAvailability, documentAvailable, extractAvailable, answersAvailable, mapAvailable,
    mapAvailabilityReason, mapAvailabilityStatus, annotatedTextColumnIds, answersCitedColumns,
    gridOnly, mapSplitShowing, graphSplitShowing, extractViewShowing, documentViewShowing,
    answersViewShowing, activePromotedView, activeWorkView, workViewSegments,
    workViewDisabledReasons, setWorkView, promoteCurrentView, openPromotedTab, closePromotedTab,
    selectSheetTab, ocrCompareOpen, ocrCompareActive, ocrCompareTarget, ocrCompareSession,
    ocrCompareCloseWarn, openOcrCompareTab, closeOcrCompareTab, setOcrCompareSession,
    setOcrCompareCloseWarn, transcribeCompareOpen, transcribeCompareActive,
    transcribeCompareSession, transcribeCompareCloseWarn, openTranscribeCompareTab,
    closeTranscribeCompareTab, setTranscribeCompareSession, setTranscribeCompareCloseWarn,
    translateCompareOpen, translateCompareActive, translateCompareSession,
    translateCompareCloseWarn, openTranslateCompareTab, closeTranslateCompareTab,
    setTranslateCompareSession, setTranslateCompareCloseWarn, topicCompareOpen, topicCompareActive,
    topicCompareSession, topicCompareCloseWarn, openTopicCompareTab, closeTopicCompareTab,
    setTopicCompareSession, setTopicCompareCloseWarn,
  }), [
    mapHidden, graphNeighborhoodHidden, evidenceMainViewHidden, sourceHealthHidden, mapColumn,
    imageGalleryAvailability, documentAvailable, extractAvailable, answersAvailable, mapAvailable,
    mapAvailabilityReason, mapAvailabilityStatus, annotatedTextColumnIds, answersCitedColumns,
    gridOnly, mapSplitShowing, graphSplitShowing, extractViewShowing, documentViewShowing,
    answersViewShowing, activePromotedView, activeWorkView, workViewSegments,
    workViewDisabledReasons, setWorkView, promoteCurrentView, openPromotedTab, closePromotedTab,
    selectSheetTab, ocrCompareOpen, ocrCompareActive, ocrCompareTarget, ocrCompareSession,
    ocrCompareCloseWarn, openOcrCompareTab, closeOcrCompareTab, setOcrCompareSession,
    setOcrCompareCloseWarn, transcribeCompareOpen, transcribeCompareActive,
    transcribeCompareSession, transcribeCompareCloseWarn, openTranscribeCompareTab,
    closeTranscribeCompareTab, setTranscribeCompareSession, setTranscribeCompareCloseWarn,
    translateCompareOpen, translateCompareActive, translateCompareSession,
    translateCompareCloseWarn, openTranslateCompareTab, closeTranslateCompareTab,
    setTranslateCompareSession, setTranslateCompareCloseWarn, topicCompareOpen, topicCompareActive,
    topicCompareSession, topicCompareCloseWarn, openTopicCompareTab, closeTopicCompareTab,
    setTopicCompareSession, setTopicCompareCloseWarn,
  ]);
}
