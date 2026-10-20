import { useCallback, useEffect, useMemo } from 'react';
import {
  ApiError, MAX_LENS_VIEW_ROWS, freshActionRequestKey,
  type ActionExecutionRequest, type GridFilterSpec, type LensResolved,
  type PreviewSampleResult, type RegisteredActionRequest, type SheetMeta,
} from '../api/open';
import { backfillWithConfirmation } from '../api/backfillWithConfirmation';
import { useDetailHandle } from '../bind/useDetailHandle';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { useJobsHandle } from '../bind/useJobsHandle';
import { useLensViewHandle } from '../bind/useLensViewHandle';
import { usePreviewViewHandle } from '../bind/usePreviewViewHandle';
import { useProjectDataResource, useRouteHandle } from '../bind/useRouteHandle';
import { useSelector } from '../bind/useSelector';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { previewViewForSheet } from '../state/previewViewStore';
import { applyLensTransition } from '../state/workspaceTransitions';
import { LENS_REFRESH_NEEDED_CODES } from './gridColumnState';

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
  const gridView = useGridViewHandle();
  const route = useRouteHandle();
  const lensViewHandle = useLensViewHandle();
  const previewViewHandle = usePreviewViewHandle();
  const { afterBackfill, requestCostConfirmation } = useJobsHandle();
  const lensView = useSelector(lensViewHandle.store, (s) => s.lensView);
  const lensOpenError = useSelector(lensViewHandle.store, (s) => s.lensOpenError);
  const previewView = useSelector(previewViewHandle.store, (s) => s.previewView);
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
    [detail, gridView, lensViewHandle, projectApi, projectData, sheet, showError],
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
    [afterBackfill, projectApi, requestCostConfirmation, showError],
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
          const page = await projectApi.getSheetData(sheet.id, offset, pageSize, { filter, columnIds: [], valueMode: 'bounded' });
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
    [afterBackfill, projectApi, requestCostConfirmation, sheet, showError],
  );

  return useMemo(() => ({
    activeLensView, lensOpenError, activePreviewView, applyLens, exitLensView, closePreviewView,
    runPreviewForReal, runWithPreview, executeRegisteredAction, runActionBackfill, resumeHaltedRun,
    retryFailedRows,
  }), [
    activeLensView, lensOpenError, activePreviewView, applyLens, exitLensView, closePreviewView,
    runPreviewForReal, runWithPreview, executeRegisteredAction, runActionBackfill, resumeHaltedRun,
    retryFailedRows,
  ]);
}
