import { X } from 'lucide-react';
import { useEffect, useRef } from 'react';
import type { ReviewRun, ReviewRunCounts } from '../../api/types';
import styles from './ReviewResultsPanel.module.css';

export interface ReviewRunSummaryProps {
  run: ReviewRun;
  /** Limits the shown facts to one selected output column. */
  fieldId?: string | null;
}

function selectedCounts(run: ReviewRun, fieldId?: string | null): ReviewRunCounts {
  return run.fields.find((field) => field.columnId === fieldId) ?? run.total;
}

function correctness(counts: ReviewRunCounts): string | null {
  const graded = counts.acceptedCount + counts.incorrectCount;
  return graded === 0 ? null : `${Math.round((counts.acceptedCount / graded) * 100)}%`;
}

/** A compact summary remains available for consumers that only need one scope. */
export function ReviewRunSummary({ run, fieldId }: ReviewRunSummaryProps) {
  const counts = selectedCounts(run, fieldId);
  const graded = counts.acceptedCount + counts.incorrectCount;
  const accuracy = correctness(counts);
  return (
    <div className={styles.summary} data-testid="review-run-summary">
      <span><strong>{counts.reviewedCount}</strong> reviewed of {counts.eligibleCount}</span>
      <span><strong>{counts.acceptedCount}</strong> accepted</span>
      <span><strong>{counts.incorrectCount}</strong> incorrect</span>
      <span>
        {accuracy === null
          ? 'No graded decisions yet'
          : `${accuracy} correct among reviewed (${counts.acceptedCount}/${graded})`}
      </span>
    </div>
  );
}

function ResultRow({ label, counts }: { label: string; counts: ReviewRunCounts }) {
  const accuracy = correctness(counts);
  return (
    <div className={styles.resultRow}>
      <div className={styles.resultLabel}>{label}</div>
      <div className={styles.metric}>
        <span className={styles.metricValue}>{counts.reviewedCount}/{counts.eligibleCount}</span>
        <span>reviewed</span>
      </div>
      <div className={styles.metric}>
        <span className={styles.metricValue}>{counts.acceptedCount}</span>
        <span>accepted</span>
      </div>
      <div className={styles.metric}>
        <span className={styles.metricValue}>{counts.incorrectCount}</span>
        <span>incorrect</span>
      </div>
      <div className={styles.metric}>
        <span className={styles.metricValue}>{accuracy ?? '—'}</span>
        <span>correct among reviewed</span>
      </div>
    </div>
  );
}

export interface ReviewResultsPanelProps {
  run: ReviewRun;
  onClose(): void;
}

/** Results intentionally describe this loaded run only; no historical trend is inferred. */
export function ReviewResultsPanel({ run, onClose }: ReviewResultsPanelProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className={`modal-card ${styles.resultsDialog}`}
      data-testid="review-results"
      aria-labelledby="review-results-title"
      onCancel={(event) => {
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }}
    >
      <header className={styles.header}>
        <div>
          <h2 className="modal-title" id="review-results-title">Results</h2>
          <p className={styles.description}>Current run: {run.actionName || run.actionKind || 'AI run'} on {run.sheetName}</p>
        </div>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close review results">
          <X size={16} aria-hidden />
        </button>
      </header>
      <section className={styles.section} aria-labelledby="review-results-overall">
        <h3 id="review-results-overall">Overall</h3>
        <ResultRow label="All output fields" counts={run.total} />
        <ReviewRunSummary run={run} />
      </section>
      <section className={styles.section} aria-labelledby="review-results-fields">
        <h3 id="review-results-fields">Output fields</h3>
        <div className={styles.fieldResults}>
          {run.fields.map((field) => <ResultRow key={field.columnId} label={field.columnName} counts={field} />)}
        </div>
      </section>
    </dialog>
  );
}
