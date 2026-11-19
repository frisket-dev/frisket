import './WorkspaceOverlays.css';
import { lazy, memo, Suspense, useCallback, useMemo } from 'react';
import { StatusBar } from '../components/StatusBar';
import { ImportFollowupNotice } from '../components/importWorkspace/ImportFollowupPrompts';
import { MENTIONS_EXTRACT_ACTION_KIND } from '../components/mentionsPanelModel';
import { useShellIdentity } from '../shellIdentity';
import { DiagnosticReportButton } from './DiagnosticReport';
import { NowPlayingWidget } from '../components/NowPlayingWidget';
import { PanelLoading } from '../components/PanelPrimitives';
import { ColumnDrawer } from '../components/ColumnDrawer';
import { ReviewQueue } from '../components/ReviewQueue';
import { canReviewProject } from '../api/projectRole';
import { reviewRunExportUrl } from '../api/raw/projectResources';
import { ProvenanceManifest } from '../components/ProvenanceManifest';
import { CompletedClusterResult } from '../components/CompletedClusterResult';
import { entityTableDraft } from '../actions/entityTable';
import { CostGateModal } from '../components/CostGateModal';
import { ConfirmDeleteRowsModal } from '../components/ConfirmDeleteRowsModal';
import { GeneratedColumnCollisionModal } from '../components/GeneratedColumnCollisionModal';
import { SupportContactNote } from '../components/SupportContactNote';
import { remediateApiError } from '../errors/remediation';
import { FeedsConnectionsDialog } from '../components/feeds/FeedsConnectionsDialog';
import { CostGateWorkbenchViewFrame, CitationWorkbenchViewFrame, GeneratedColumnCollisionWorkbenchViewFrame, ProvenanceWorkbenchPanelFrame, ResolvedWorkbenchLayoutRegion, ReviewQueueWorkbenchViewFrame, RowDeleteConfirmWorkbenchViewFrame } from '../workbench/contributions';
import { type WorkbenchResolvedLayoutContribution } from '../workbench/layout';
import { PluginPeekHost } from '../workbench/PluginPeekHost';
import { WorkbenchCommandPalette } from '../workbench/WorkbenchCommandPalette';
import { useChromeHandle } from '../bind/useChromeHandle';
import { useActSurfaceHandle } from '../bind/useActSurfaceHandle';
import { useJobsHandle } from '../bind/useJobsHandle';
import { useSelector } from '../bind/useSelector';
import { useWorkspaceShell, useOverlayModel } from './WorkspaceContext';
import { useWorkspaceNavigationTelemetry, type WorkspaceTelemetryPanel } from '../telemetry/navigationTelemetry';

export const LazyCitationViewer = lazy(() => (
  import('../components/CitationViewer').then(({ CitationViewer }) => ({ default: CitationViewer }))
));

export const WorkspaceOverlayRegion = memo(function WorkspaceOverlayRegion() {
  const { identityMode } = useShellIdentity();
  const overlayChrome = useChromeHandle();
  const projectionStatus = useSelector(overlayChrome.store, (s) => s.projectionStatus);
  const {
    invalidateProjectData,
    openFeedHealthMainView,
    projectApi,
    refreshHistory,
    refreshSheets,
  } = useWorkspaceShell();
  const {
    afterColumnUpdated,
    applyGridFailedFilter,
    retryFailedRows,
    clearDeleteRowsConfirm,
    closeCitationViewer,
    closeReview,
    closeRoutePanel,
    columnDrawer,
    commandPaletteOpen,
    closeCommandPaletteAndReset,
    commandPaletteQuery,
    commandPaletteSearchSeed,
    setCommandPaletteQuery,
    commandPaletteBestMatchItems,
    commandPaletteActionItems,
    commandPaletteGotoItems,
    navigateToSearchHit,
    closeRowDrawer,
    runActionFromSurface,
    selectSheet,
    sheets,
    confirmDeleteRows,
    deleteRowsConfirm,
    dismissPluginPeek,
    doRedo,
    doUndo,
    error,
    citationViewerState,
    documentPriority,
    hideWorkbenchContribution,
    history,
    lastCommandAction,
    openCitationViewer,
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
    sheet,
    commitReviewDecision,
    workbenchCommandEntries,
    workbenchHostContext,
    workbenchVisibilityTargets,
    importProgress,
    importFollowup,
  } = useOverlayModel();
  const visiblePanels: WorkspaceTelemetryPanel[] = [];
  if (columnDrawer && sheet) visiblePanels.push('column');
  if (citationViewerState?.host === 'modalOrPeek') visiblePanels.push('evidence');
  useWorkspaceNavigationTelemetry({ projectId: project.id, rightPanels: visiblePanels });
  const actSurfaceHandle = useActSurfaceHandle();
  const feedsConnectionsOpen = useSelector(
    actSurfaceHandle.store,
    (state) => state.feedsConnectionsOpen,
  );
  const jobs = useJobsHandle();
  const run = useSelector(jobs.store, (state) => state.run);
  const costGate = useSelector(jobs.store, (state) => state.costGate);
  const generatedColumnCollision = useSelector(
    jobs.store,
    (state) => state.generatedColumnCollision,
  );
  const afterBackfill = jobs.afterBackfill;
  const cancelCostGate = jobs.cancelCostGate;
  const cancelCurrentRun = jobs.cancelCurrentRun;
  const confirmCostGate = jobs.confirmCostGate;
  const cancelGeneratedColumnCollision = jobs.cancelGeneratedColumnCollision;
  const confirmGeneratedColumnCollision = jobs.confirmGeneratedColumnCollision;
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

  const pendingFollowup = importFollowup.pending;
  const followupNotice = pendingFollowup && sheet && Number(sheet.id) === pendingFollowup.sheetId ? (
    <ImportFollowupNotice
      followup={pendingFollowup.followup}
      onRun={() => {
        const { followup } = pendingFollowup;
        importFollowup.dismiss();
        if (followup.kind === 'download') runActionFromSurface(followup.launcherKind, followup.columnName);
        else runActionFromSurface(MENTIONS_EXTRACT_ACTION_KIND);
      }}
      onDismiss={importFollowup.dismiss}
    />
  ) : null;

  return (
<>
      <StatusBar
        actions={identityMode ? <DiagnosticReportButton /> : undefined}
        importFollowup={followupNotice}
        run={run}
        history={history}
        reviewCount={reviewCount}
        projectionStatus={projectionStatus}
        importProgress={importProgress}
        importPriority={documentPriority}
        onUndo={doUndo}
        onRedo={doRedo}
        onOpenReview={() => openReview()}
        onCancelRun={cancelCurrentRun}
        onShowFailedRows={applyGridFailedFilter}
        onRetryFailedRows={retryFailedRows}
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
      <FeedsConnectionsDialog
        open={feedsConnectionsOpen}
        feedApi={projectApi}
        jobs={jobs}
        onClose={actSurfaceHandle.closeSourcesConnections}
        onOpenImportDialog={() => actSurfaceHandle.openImportDialog('feed')}
        onPolled={(sheetId) => {
          void refreshSheets().then(() => {
            if (sheetId != null) selectSheet(String(sheetId));
          });
          void refreshHistory();
          invalidateProjectData();
        }}
        onOpenFeedHealthMainView={(sourceId) => {
          actSurfaceHandle.closeSourcesConnections();
          openFeedHealthMainView(sourceId);
        }}
        feedDetailContributions={resolvedRegion('sourceDetail').contributions}
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
          activeSearchSheetId={sheet?.id}
          searchSeed={commandPaletteSearchSeed}
          onNavigateSearchHit={navigateToSearchHit}
          projectId={project.id}
          onOpenJobs={() => overlayChrome.setActiveBottomDockTab('jobs')}
          launcherCommands={paletteLauncherCommands}
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

      {generatedColumnCollision && (
        <GeneratedColumnCollisionWorkbenchViewFrame>
          <GeneratedColumnCollisionModal
            columns={generatedColumnCollision.columns}
            message={generatedColumnCollision.message}
            onCancel={cancelGeneratedColumnCollision}
            onConfirm={confirmGeneratedColumnCollision}
          />
        </GeneratedColumnCollisionWorkbenchViewFrame>
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
      {citationViewerState?.host === 'modalOrPeek' && (
        <CitationWorkbenchViewFrame host="modalOrPeek">
          <Suspense
            fallback={
              <PanelLoading
                className="panel-loading-page"
                testId="evidence-viewer-loading"
                label="Loading citation…"
              />
            }
          >
            <LazyCitationViewer
              key={`${String(citationViewerState.linkId)}:${citationViewerState.scopeSpanId ?? ''}`}
              evidenceLinkId={citationViewerState.linkId}
              initialPayload={citationViewerState.initialPayload}
              initialPage={citationViewerState.initialPage}
              scopeSpanId={citationViewerState.scopeSpanId}
              scopeSpanIds={citationViewerState.scopeSpanIds}
              highlight={citationViewerState.highlight}
              onClose={closeCitationViewer}
              onOpenCompanion={() => {
                closeRowDrawer();
                openCitationViewer(citationViewerState.linkId, 'mainView', {
                  initialPayload: citationViewerState.initialPayload,
                  initialPage: citationViewerState.initialPage,
                  scopeSpanId: citationViewerState.scopeSpanId,
                  scopeSpanIds: citationViewerState.scopeSpanIds,
                  highlight: citationViewerState.highlight,
                });
              }}
            />
          </Suspense>
        </CitationWorkbenchViewFrame>
      )}
      {reviewOpen && (
        <ReviewQueueWorkbenchViewFrame host="modalOrPeek">
          <ReviewQueue
            onClose={closeReview}
            onChanged={commitReviewDecision}
            runId={reviewRunId ?? undefined}
            reviewExportUrl={canReviewProject(project) ? (id) => reviewRunExportUrl(project.id, id) : undefined}
          />
        </ReviewQueueWorkbenchViewFrame>
      )}
  </>
  );
});
