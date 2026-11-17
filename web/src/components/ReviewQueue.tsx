import './ReviewQueue.css';
import { ReapplyResultsDialog } from './review/ReapplyResultsDialog';
import { Button } from './Button';
import { retryWithConfirmation } from '../api/retryWithConfirmation';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useJobsHandle } from '../bind/useJobsHandle';
import { useProjectDataResource } from '../bind/useRouteHandle';
import { useMemo, useState, type ReactNode } from 'react';
import type { ReviewBundleOrder } from '../api/types';
import { ReviewRunControls } from './review/ReviewRunControls';
import { ReviewSession } from './review/ReviewSession';
import { ReviewHeader, ReviewShell } from './review/ReviewPresentation';
import { useReviewRuns } from './review/useReviewRuns';

interface ReviewQueueProps {
  runId?: string;
  reviewExportUrl?(runId: string): string;
  onClose(): void;
  onChanged(remaining: number): void;
}

/** Run selection and completion sit above the keyed, per-run review session. */
export function ReviewQueue({ onClose, onChanged, runId, reviewExportUrl }: ReviewQueueProps) {
  const runs = useReviewRuns(runId);
  const { projectApi } = useWorkspaceStores();
  const { requestCostConfirmation, afterBackfill } = useJobsHandle();
  const projectData = useProjectDataResource();
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [reapplyOpen, setReapplyOpen] = useState(false);
  const [fieldId, setFieldId] = useState<string | null>(null);
  const [order, setOrder] = useState<ReviewBundleOrder>('shuffle');
  const selectedRun = runs.runs.find((run) => run.sourceId === runs.selectedRunId);
  const selectedField = selectedRun?.fields.find((field) => field.columnId === fieldId);
  const hasConfidence = (selectedField ?? selectedRun?.total)?.confidenceCount;
  const effectiveOrder = order === 'confidence' && !hasConfidence ? 'shuffle' : order;
  const options = useMemo(() => ({
    fieldId: fieldId ?? undefined, order: effectiveOrder,
  }), [effectiveOrder, fieldId]);

  const retry = async () => {
    if (!selectedRun?.runId || actionBusy) return;
    setActionBusy(true);
    setActionError(null);
    try {
      const result = await retryWithConfirmation(projectApi, requestCostConfirmation,
        selectedRun.sheetId, selectedRun.runId);
      if (result?.runId) { afterBackfill(result.runId); onClose(); }
    } catch (error) { setActionError(error instanceof Error ? error.message : String(error)); }
    finally { setActionBusy(false); }
  };
  const reapplyDialog = reapplyOpen && selectedRun && <ReapplyResultsDialog run={selectedRun}
    onClose={() => setReapplyOpen(false)} onApplied={() => {
      setReapplyOpen(false);
      projectData.invalidate();
      onChanged(selectedRun.total.unreviewedCount);
    }} />;

  const toolbar = (navigation: ReactNode, leave: (action: () => void) => void, busy: boolean) => (
    <ReviewHeader busy={busy} onClose={() => leave(onClose)}>
      <ReviewRunControls runs={runs.runs} selectedRunId={runs.selectedRunId}
        onSelectedRunChange={(id) => leave(() => { runs.setSelectedRunId(id); setFieldId(null); })}
        selectedFieldId={fieldId} onSelectedFieldChange={(id) => leave(() => setFieldId(id))}
        order={effectiveOrder} onOrderChange={(value) => leave(() => setOrder(value))} statusBusy={runs.statusBusy}
        disabled={busy} hasMore={runs.hasMore} loadingMore={runs.loadingMore}
        onLoadMore={() => { void runs.loadMore(); }}
        reviewExportUrl={reviewExportUrl}
        onRunStatusChange={(_run, status) => leave(() => { void runs.setRunStatus(status); })}
        resultActions={(close) => selectedRun?.runId && <>
          {selectedRun.retrySupported !== false && <Button type="button" disabled={busy || actionBusy}
            onClick={() => { close(); leave(() => { void retry(); }); }}>Retry unfinished</Button>}
          <Button type="button" disabled={busy || actionBusy}
            onClick={() => { close(); leave(() => { setActionError(null); setReapplyOpen(true); }); }}>Reapply saved results</Button>
        </>}>
        {navigation}
      </ReviewRunControls>
    </ReviewHeader>
  );

  if (!runs.loading && selectedRun) return <ReviewSession
    key={`${selectedRun.sourceId}:${fieldId ?? ''}:${effectiveOrder}`}
    runId={selectedRun.sourceId} options={options} readOnly={selectedRun.reviewStatus === 'complete' || runs.statusBusy}
    onChanged={onChanged} onClose={onClose} renderToolbar={toolbar}
    renderFrame={(content, leave, busy, editing) => <ReviewShell busy={busy} onClose={() => { if (!editing) leave(onClose); }}>
      {runs.error && <p className="panel-error review-load-error" role="alert">{runs.error}</p>}
      {actionError && !reapplyOpen && <p className="panel-error" role="alert">{actionError}</p>}
      {content}{reapplyDialog}
    </ReviewShell>}
    onDecisionSaved={() => { void runs.refreshRun(selectedRun.sourceId); }} />;

  return <ReviewShell busy={false} onClose={onClose}>
    {toolbar(null, (action) => action(), runs.loading)}
    {runs.error && <p className="panel-error review-load-error" role="alert">{runs.error}</p>}
    {runs.loading ? <div className="review-empty">Loading runs…</div>
      : <div className="review-empty" data-testid="review-empty">No results to review yet.</div>}
  </ReviewShell>;
}
