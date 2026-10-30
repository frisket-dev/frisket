import './ReviewQueue.css';
import { useMemo, useState, type ReactNode } from 'react';
import type { ReviewBundleOrder } from '../api/types';
import { useEscapeDismiss } from '../hooks/useEscapeDismiss';
import { ReviewRunControls } from './review/ReviewRunControls';
import { ReviewSession } from './review/ReviewSession';
import { ReviewHeader, ReviewShell } from './review/ReviewPresentation';
import { useReviewRuns } from './review/useReviewRuns';

export interface ReviewQueueProps {
  runId?: string;
  reviewExportUrl?(runId: string): string;
  onClose(): void;
  onChanged(remaining: number): void;
}

/** Run selection and completion sit above the keyed, per-run review session. */
export function ReviewQueue({ onClose, onChanged, runId, reviewExportUrl }: ReviewQueueProps) {
  const runs = useReviewRuns(runId);
  const [fieldId, setFieldId] = useState<string | null>(null);
  const [order, setOrder] = useState<ReviewBundleOrder>('shuffle');
  const selectedRun = runs.runs.find((run) => run.runId === runs.selectedRunId);
  const selectedField = selectedRun?.fields.find((field) => field.columnId === fieldId);
  const hasConfidence = (selectedField ?? selectedRun?.total)?.confidenceCount;
  const effectiveOrder = order === 'confidence' && !hasConfidence ? 'shuffle' : order;
  const options = useMemo(() => ({
    fieldId: fieldId ?? undefined, order: effectiveOrder,
  }), [effectiveOrder, fieldId]);
  useEscapeDismiss(onClose, { typingGuard: true, enabled: !selectedRun });

  const toolbar = (navigation: ReactNode, leave: (action: () => void) => void, busy: boolean) => (
    <ReviewHeader busy={busy} onClose={() => leave(onClose)}>
      <ReviewRunControls runs={runs.runs} selectedRunId={runs.selectedRunId}
        onSelectedRunChange={(id) => leave(() => { runs.setSelectedRunId(id); setFieldId(null); })}
        selectedFieldId={fieldId} onSelectedFieldChange={(id) => leave(() => setFieldId(id))}
        order={effectiveOrder} onOrderChange={(value) => leave(() => setOrder(value))} statusBusy={runs.statusBusy}
        disabled={busy} hasMore={runs.hasMore} loadingMore={runs.loadingMore}
        onLoadMore={() => { void runs.loadMore(); }}
        reviewExportUrl={reviewExportUrl}
        onRunStatusChange={(_run, status) => leave(() => { void runs.setRunStatus(status); })}>
        {navigation}
      </ReviewRunControls>
    </ReviewHeader>
  );

  return <ReviewShell>
    {!selectedRun && toolbar(null, (action) => action(), runs.loading)}
    {runs.error && <p className="panel-error review-load-error" role="alert">{runs.error}</p>}
    {runs.loading ? <div className="review-empty">Loading runs…</div>
      : selectedRun ? <ReviewSession
        key={`${selectedRun.runId}:${fieldId ?? ''}:${effectiveOrder}`}
        runId={selectedRun.runId} options={options} readOnly={selectedRun.reviewStatus === 'complete' || runs.statusBusy}
        onChanged={onChanged} onClose={onClose} renderToolbar={toolbar}
        onDecisionSaved={() => { void runs.refreshRun(selectedRun.runId); }} />
        : <div className="review-empty" data-testid="review-empty">No results to review yet.</div>}
  </ReviewShell>;
}
