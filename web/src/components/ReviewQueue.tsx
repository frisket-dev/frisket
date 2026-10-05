import { useMemo, useState, type ReactNode } from 'react';
import { X } from 'lucide-react';
import type { ReviewBundleOrder } from '../api/types';
import { useEscapeDismiss } from '../hooks/useEscapeDismiss';
import { ReviewRunControls } from './review/ReviewRunControls';
import { ReviewSession } from './review/ReviewSession';
import { useReviewRuns } from './review/useReviewRuns';

export interface ReviewQueueProps {
  runId?: string;
  onClose(): void;
  onChanged(remaining: number): void;
}

/** Run selection and completion sit above the keyed, per-run review session. */
export function ReviewQueue({ onClose, onChanged, runId }: ReviewQueueProps) {
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
    <div className="review-run-header">
      <ReviewRunControls runs={runs.runs} selectedRunId={runs.selectedRunId}
        onSelectedRunChange={(id) => leave(() => { runs.setSelectedRunId(id); setFieldId(null); })}
        selectedFieldId={fieldId} onSelectedFieldChange={(id) => leave(() => setFieldId(id))}
        order={effectiveOrder} onOrderChange={(value) => leave(() => setOrder(value))} statusBusy={runs.statusBusy}
        disabled={busy} hasMore={runs.hasMore} loadingMore={runs.loadingMore}
        onLoadMore={() => { void runs.loadMore(); }}
        onRunStatusChange={(_run, status) => leave(() => { void runs.setRunStatus(status); })}>
        {navigation}
      </ReviewRunControls>
      <button type="button" className="icon-btn" disabled={busy} onClick={() => leave(onClose)} aria-label="Close review queue"><X size={16} /></button>
    </div>
  );

  return <dialog className="review-overlay" data-testid="review-queue" aria-label="Review queue" open
    onCancel={(event) => { event.preventDefault(); }}>
    {!selectedRun && toolbar(null, (action) => action(), runs.loading)}
    {runs.error && <p className="form-error review-load-error" role="alert">{runs.error}</p>}
    {runs.loading ? <div className="review-empty">Loading runs…</div>
      : selectedRun ? <ReviewSession
        key={`${selectedRun.runId}:${fieldId ?? ''}:${effectiveOrder}`}
        runId={selectedRun.runId} options={options} readOnly={selectedRun.reviewStatus === 'complete' || runs.statusBusy}
        onChanged={onChanged} onClose={onClose} renderToolbar={toolbar}
        onDecisionSaved={() => { void runs.refreshRun(selectedRun.runId); }} />
        : <div className="review-empty" data-testid="review-empty">No results to review yet.</div>}
  </dialog>;
}
