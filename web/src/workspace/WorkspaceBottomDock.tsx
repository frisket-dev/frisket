import { memo, useCallback, useMemo } from 'react';
import { BottomDockTabFrame, ResolvedWorkbenchLayoutRegion, HistoryWorkbenchPanel } from '../workbench/contributions';
import { BOTTOM_DOCK_ERRORS_DESCRIPTOR, BOTTOM_DOCK_JOBS_DESCRIPTOR, BOTTOM_DOCK_LINEAGE_DESCRIPTOR } from '../workbench/descriptors';
import { type WorkbenchResolvedLayoutContribution } from '../workbench/layout';
import { PluginDockTabHost } from '../workbench/PluginDockTabHost';
import { WorkbenchBottomDock, WorkbenchJobSplitPanel, type DockActionJob, type WorkbenchBottomDockRenderContext } from '../workbench/WorkbenchBottomDock';
import { deriveDockErrorJobs, deriveDockRunSummary, derivePluginErrorJobs } from '../workbench/dockJobSummary';
import { LineagePanel } from '../workbench/LineagePanel';
import { useProjectDataResource } from '../bind/useRouteHandle';
import { useChromeHandle } from '../bind/useChromeHandle';
import { useJobsHandle } from '../bind/useJobsHandle';
import { usePluginLayoutHandle } from '../bind/usePluginLayoutHandle';
import { useSelector } from '../bind/useSelector';
import { useRenderCount, useWorkspaceShell, useWorkbenchHostContext, useResolvedWorkbenchLayout, useCurrentSheet, resolveRegionContributions } from './WorkspaceContext';

export const WorkspaceBottomDockRegion = memo(function WorkspaceBottomDockRegion() {
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
          liveActionJobs={job.liveActionJobs}
          runSummary={runSummary}
          tabBadges={tabBadges}
        />
      </ResolvedWorkbenchLayoutRegion>
  );
});
