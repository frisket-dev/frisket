import { useCallback, useEffect, useMemo } from 'react';
import {
  freshActionRequestKey,
  type ActionExecutionRequest,
  type PreviewSampleResult, type RegisteredActionRequest, type SheetMeta,
} from '../api/open';
import { backfillWithConfirmation } from '../api/backfillWithConfirmation';
import { retryWithConfirmation, retryFailedRowsWithConfirmation } from '../api/retryWithConfirmation';
import { useDetailHandle } from '../bind/useDetailHandle';
import { useJobsHandle } from '../bind/useJobsHandle';
import { usePreviewViewHandle } from '../bind/usePreviewViewHandle';
import { useProjectDataResource, useRouteHandle } from '../bind/useRouteHandle';
import { useSelector } from '../bind/useSelector';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { previewViewForSheet } from '../state/previewViewStore';

/** Launches through the existing jobs and preview resources, including their consent and generation fences. */
export function useWorkspaceActionController({
  sheet, closeRoutePanel, startRun, showError,
}: {
  sheet: SheetMeta | undefined;
  closeRoutePanel: () => void;
  startRun: (request: ActionExecutionRequest) => void;
  showError: (message: string) => void;
}) {
  const { projectApi } = useWorkspaceStores();
  const projectData = useProjectDataResource();
  const detail = useDetailHandle();
  const route = useRouteHandle();
  const previewViewHandle = usePreviewViewHandle();
  const { afterBackfill, requestCostConfirmation } = useJobsHandle();
  const previewView = useSelector(previewViewHandle.store, (s) => s.previewView);
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
    [afterBackfill, projectApi, requestCostConfirmation, showError],
  );

  const runActionBackfill = useCallback(
    (targetColumnName: string, rowIds?: number[]) => {
      if (!sheet) return Promise.resolve();
      return runColumnBackfill(sheet.id, targetColumnName, rowIds);
    },
    [runColumnBackfill, sheet],
  );

  const resumeHaltedRun = useCallback(async (sheetId: string, sourceRunId: string) => {
    try {
      const result = await retryWithConfirmation(projectApi, requestCostConfirmation, sheetId, sourceRunId);
      if (result?.runId) afterBackfill(result.runId);
    } catch (error) { showError(error instanceof Error ? error.message : String(error)); }
  }, [afterBackfill, projectApi, requestCostConfirmation, showError]);

  const retryFailedRows = useCallback(
    async (targetColumnName: string, outcome: string): Promise<void> => {
      if (!sheet) return;
      try {
        const column = sheet.columns.find((candidate) => candidate.name === targetColumnName);
        if (!column) return;
        await retryFailedRowsWithConfirmation(
          projectApi, requestCostConfirmation, sheet.id, column, outcome, afterBackfill,
        );
      } catch (e: unknown) {
        showError(e instanceof Error ? e.message : String(e));
      }
    },
    [afterBackfill, projectApi, requestCostConfirmation, sheet, showError],
  );

  return useMemo(() => ({
    activePreviewView, closePreviewView,
    runPreviewForReal, runWithPreview, executeRegisteredAction, runActionBackfill, resumeHaltedRun,
    retryFailedRows,
  }), [
    activePreviewView, closePreviewView,
    runPreviewForReal, runWithPreview, executeRegisteredAction, runActionBackfill, resumeHaltedRun,
    retryFailedRows,
  ]);
}
