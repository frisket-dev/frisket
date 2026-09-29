import { useMemo, useState } from 'react';
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
  const [seed] = useState(() => Math.floor(Math.random() * 2147483647));
  const selectedRun = runs.runs.find((run) => run.runId === runs.selectedRunId);
  const selectedField = selectedRun?.fields.find((field) => field.columnId === fieldId);
  const hasConfidence = (selectedField ?? selectedRun?.total)?.confidenceCount;
  const effectiveOrder = hasConfidence ? order : 'shuffle';
  const options = useMemo(() => ({
    fieldId: fieldId ?? undefined, order: effectiveOrder, seed,
  }), [effectiveOrder, fieldId, seed]);
  useEscapeDismiss(onClose, { typingGuard: true });

  return <dialog className="review-overlay" data-testid="review-queue" aria-label="Review queue" open
    onCancel={(event) => { event.preventDefault(); onClose(); }}>
    <div className="review-run-header">
      <ReviewRunControls runs={runs.runs} selectedRunId={runs.selectedRunId}
        onSelectedRunChange={(id) => { runs.setSelectedRunId(id); setFieldId(null); }}
        selectedFieldId={fieldId} onSelectedFieldChange={setFieldId}
        order={effectiveOrder} onOrderChange={setOrder} statusBusy={runs.statusBusy}
        onRunStatusChange={(_run, status) => { void runs.setRunStatus(status); }} />
      <button type="button" className="icon-btn" onClick={onClose} aria-label="Close review queue">
        <X size={16} />
      </button>
    </div>
    {runs.hasMore && <button type="button" className="btn review-load-runs"
      onClick={() => { void runs.loadMore(); }} disabled={runs.loadingMore}>
      {runs.loadingMore ? 'Loading…' : 'Load older runs'}
    </button>}
    {runs.error && <p className="form-error review-load-error" role="alert">{runs.error}</p>}
    {runs.loading ? <div className="review-empty">Loading runs…</div>
      : selectedRun ? <ReviewSession
        key={`${selectedRun.runId}:${fieldId ?? ''}:${effectiveOrder}`}
        runId={selectedRun.runId} options={options} readOnly={selectedRun.reviewStatus === 'complete'}
        onChanged={onChanged}
        onDecisionSaved={() => { void runs.refreshRun(selectedRun.runId); }} />
        : <div className="review-empty" data-testid="review-empty">No results to review yet.</div>}
  </dialog>;
}
