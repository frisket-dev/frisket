import '../workbench/contributions.css';
import './WorkspaceMainView.css';
import '../grid/SheetGrid.css';
import { WorkspaceActionCompareView, WorkspaceActionCompareHistory } from './WorkspaceActionCompareView';
import { CompareDiscardDialog } from '../workbench/CompareDiscardDialog';
import { memo } from 'react';
import { ImportDropzone } from '../components/importWorkspace/ImportDropzone';
import { SourceHealthMainView } from '../components/sources/SourceHealthMainView';
import { ResolvedWorkbenchLayoutRegion, SourceHealthMainViewFrame } from '../workbench/contributions';
import { OcrCompareTab } from '../workbench/OcrCompareTab';
import { TranscribeCompareTab } from '../workbench/TranscribeCompareTab';
import { TranslateCompareTab } from '../workbench/TranslateCompareTab';
import { TopicSegmentationCompareTab } from '../workbench/TopicSegmentationCompareTab';
import { EVIDENCE_CONTRIBUTION_ID, GRAPH_NEIGHBORHOOD_CONTRIBUTION_ID, SOURCE_HEALTH_CONTRIBUTION_ID } from './contributionIds';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useActSurfaceHandle } from '../bind/useActSurfaceHandle';
import { useActionCatalogHandle } from '../bind/useActionCatalogHandle';
import { useSelector } from '../bind/useSelector';
import { useRenderCount, useMainViewModel } from './WorkspaceContext';
import { WorkspaceMainViewTabs } from './WorkspaceSheetTabs';
import { WorkspacePromotedView, WorkspacePrimarySurface } from './WorkspaceSurface';
import { WorkspaceSheetToolbar, WorkspaceGridControls, WorkspaceInlineFilterRow, WorkspaceGridBanners } from './WorkspaceToolbar';
import { WorkspaceHeaderMenuSlot } from './WorkspaceHeaderMenu';

export const WorkspaceNavigateRegion = memo(function WorkspaceNavigateRegion() {
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

export const WorkspaceMainViewRegion = memo(function WorkspaceMainViewRegion() {
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
    actionCompareOpen,
    actionCompareActive,
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
      <WorkspaceActionCompareHistory />
      {actionCompareOpen && <div className="workbench-ocr-compare-keepalive" hidden={!actionCompareActive}>
        <WorkspaceActionCompareView />
      </div>}
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
        !actionCompareActive &&
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
  return (
    <div className="workbench-ocr-compare-view" data-testid="workbench-ocr-compare-view">
      <OcrCompareTab active={ocrCompareActive} target={ocrCompareTarget} onSessionChange={setOcrCompareSession} />
      {ocrCompareCloseWarn && <CompareDiscardDialog
        title="Discard this OCR comparison?"
        description="Leaving discards the preview text and your votes — nothing is saved to the project."
        verdict={ocrCompareSession.verdict} testId="ocr-compare"
        onCancel={() => setOcrCompareCloseWarn(false)} onDiscard={closeOcrCompareTab} />}
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
  return (
    <div className="workbench-ocr-compare-view" data-testid="workbench-transcribe-compare-view">
      <TranscribeCompareTab active={transcribeCompareActive} onSessionChange={setTranscribeCompareSession} />
      {transcribeCompareCloseWarn && <CompareDiscardDialog
        description="Leaving discards the transcripts and your votes — nothing is saved to the project."
        verdict={transcribeCompareSession.verdict} testId="transcribe-compare"
        onCancel={() => setTranscribeCompareCloseWarn(false)} onDiscard={closeTranscribeCompareTab} />}
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
  return (
    <div className="workbench-ocr-compare-view" data-testid="workbench-translate-compare-view">
      <TranslateCompareTab onSessionChange={setTranslateCompareSession} />
      {translateCompareCloseWarn && <CompareDiscardDialog
        description="Leaving discards the sample translations — nothing is saved to the project."
        verdict={translateCompareSession.verdict} testId="translate-compare"
        onCancel={() => setTranslateCompareCloseWarn(false)} onDiscard={closeTranslateCompareTab} />}
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
  return (
    <div className="workbench-ocr-compare-view" data-testid="workbench-topic-compare-view">
      <TopicSegmentationCompareTab onSessionChange={setTopicCompareSession} />
      {topicCompareCloseWarn && <CompareDiscardDialog
        description="Leaving discards the topic sections and your votes — nothing is saved to the project."
        verdict={topicCompareSession.verdict} testId="topic-compare"
        onCancel={() => setTopicCompareCloseWarn(false)} onDiscard={closeTopicCompareTab} />}
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
              onOpenTable={actSurface.openTableImport}
              onOpenBulk={actSurface.openBulkImport}
              ftmImportEnabled={ftmImportEnabled}
            />
  );
}
