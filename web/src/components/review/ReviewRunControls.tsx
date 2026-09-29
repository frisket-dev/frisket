import type {
  ReviewBundleOrder,
  ReviewRun,
  ReviewRunCounts,
  ReviewRunField,
  ReviewRunStatus,
} from '../../api/types';
import { PanelSelect, type PanelSelectOption } from '../PanelSelect';
import styles from './ReviewRunControls.module.css';

export interface ReviewRunSummaryProps {
  run: ReviewRun;
  /** Limits the shown facts to one selected output column. */
  fieldId?: string | null;
}

function selectedCounts(run: ReviewRun, fieldId?: string | null): ReviewRunCounts {
  return run.fields.find((field) => field.columnId === fieldId) ?? run.total;
}

function runLabel(run: ReviewRun): string {
  const action = run.actionName || run.actionKind || 'AI run';
  return `${action} · ${run.sheetName} · run #${run.runId}`;
}

function countDescription(counts: ReviewRunCounts): string {
  return `${counts.reviewedCount}/${counts.eligibleCount} reviewed`;
}

type ReviewProgress = 'complete' | 'inProgress' | 'unreviewed';

function reviewProgress(run: ReviewRun): { state: ReviewProgress; label: string } {
  if (run.reviewStatus === 'complete') return { state: 'complete', label: 'Review complete' };
  if (run.total.reviewedCount > 0) return { state: 'inProgress', label: 'Review in progress' };
  return { state: 'unreviewed', label: 'No review decisions yet' };
}

function ReviewProgressDot({ run }: { run: ReviewRun }) {
  const { state, label } = reviewProgress(run);
  return (
    <span
      className={`${styles.reviewProgressDot} ${styles[`reviewProgressDot${state}`]}`}
      role="img"
      aria-label={label}
      title={label}
    />
  );
}

function fieldOption(field: ReviewRunField): PanelSelectOption {
  return {
    value: field.columnId,
    label: field.columnName,
    description: countDescription(field),
  };
}

/** Displays only durable observed counts. It intentionally never estimates a
 * whole-run accuracy from the reviewed subset. */
export function ReviewRunSummary({ run, fieldId }: ReviewRunSummaryProps) {
  const counts = selectedCounts(run, fieldId);
  const graded = counts.acceptedCount + counts.incorrectCount;
  const accuracy = graded === 0 ? null : Math.round((counts.acceptedCount / graded) * 100);
  return (
    <div className={styles.summary} data-testid="review-run-summary">
      <span><strong>{counts.reviewedCount}</strong> reviewed of {counts.eligibleCount}</span>
      <span><strong>{counts.acceptedCount}</strong> accepted</span>
      <span><strong>{counts.incorrectCount}</strong> incorrect</span>
      <span>
        {accuracy === null
          ? 'No graded decisions yet'
          : `${accuracy}% correct among reviewed (${counts.acceptedCount}/${graded})`}
      </span>
    </div>
  );
}

export interface ReviewRunControlsProps {
  /** All pages loaded so far. Paging and a pinned older run stay with the queue owner. */
  runs: readonly ReviewRun[];
  selectedRunId?: string | null;
  onSelectedRunChange(runId: string): void;
  selectedFieldId?: string | null;
  onSelectedFieldChange(fieldId: string | null): void;
  order: ReviewBundleOrder;
  onOrderChange(order: ReviewBundleOrder): void;
  /** The queue owner persists this separately from individual decisions. */
  onRunStatusChange?(run: ReviewRun, status: ReviewRunStatus): void;
  statusBusy?: boolean;
  disabled?: boolean;
}

/** Controlled run, output-field and ordering controls for ReviewQueue. The
 * caller owns data loading, queue reset, errors and random seed continuity. */
export function ReviewRunControls({
  runs,
  selectedRunId,
  onSelectedRunChange,
  selectedFieldId,
  onSelectedFieldChange,
  order,
  onOrderChange,
  onRunStatusChange,
  statusBusy = false,
  disabled = false,
}: ReviewRunControlsProps) {
  const run = runs.find((candidate) => candidate.runId === selectedRunId);
  const counts = run ? selectedCounts(run, selectedFieldId) : null;
  const confidenceAvailable = (counts?.confidenceCount ?? 0) > 0;
  const runOptions: PanelSelectOption[] = runs.map((candidate) => ({
    value: candidate.runId,
    label: runLabel(candidate),
    description: `${reviewProgress(candidate).label} · ${countDescription(candidate.total)}`,
    leadingIcon: <ReviewProgressDot run={candidate} />,
  }));
  // A selected, older run can arrive via a separate targeted request while a
  // normal page is loading. Keep its value in the native select in the interim.
  if (selectedRunId && !run) {
    runOptions.unshift({ value: selectedRunId, label: `Run ${selectedRunId}`, description: 'Loading run details…' });
  }
  const fieldOptions = run ? [
    { value: '', label: 'All output fields', description: countDescription(run.total) },
    ...run.fields.map(fieldOption),
  ] : [];
  const status = run?.reviewStatus;
  const nextStatus: ReviewRunStatus | undefined = status === 'complete' ? 'open' : 'complete';

  return (
    <section className={styles.controls} aria-label="Review scope" data-testid="review-run-controls">
      <div className={styles.pickerRow}>
        <label className={styles.label}>
          Run
          <PanelSelect
            className={`form-input ${styles.select}`}
            value={selectedRunId ?? ''}
            onValueChange={onSelectedRunChange}
            options={runOptions}
            ariaLabel={run ? `Run: ${reviewProgress(run).label}` : 'Run'}
            disabled={disabled || runOptions.length === 0}
            emptyMessage="No reviewable runs"
            testId="review-run-select"
          />
        </label>
        <label className={styles.label}>
          Output field
          <PanelSelect
            className={`form-input ${styles.select}`}
            value={selectedFieldId ?? ''}
            onValueChange={(value) => onSelectedFieldChange(value || null)}
            options={fieldOptions}
            disabled={disabled || !run}
            emptyMessage="No output fields"
            testId="review-field-select"
          />
        </label>
      </div>

      <div className={styles.bottomRow}>
        {run && <ReviewRunSummary run={run} fieldId={selectedFieldId} />}
        <div className={styles.actions}>
          <span className="muted">Order</span>
          <div className={styles.order} role="group" aria-label="Review order">
            <button
              type="button"
              aria-pressed={order === 'shuffle'}
              onClick={() => onOrderChange('shuffle')}
              disabled={disabled || !run}
            >
              Random
            </button>
            <button
              type="button"
              aria-pressed={order === 'confidence'}
              onClick={() => onOrderChange('confidence')}
              disabled={disabled || !run || !confidenceAvailable}
              title={confidenceAvailable ? undefined : 'Lowest confidence is unavailable because this scope has no confidence values.'}
            >
              Lowest confidence
            </button>
          </div>
          {!confidenceAvailable && run && (
            <span className="muted">No confidence values in this scope</span>
          )}
          {run && status === 'complete' && (
            <span className={`${styles.status} ${styles.statusComplete}`}>
              {status}
            </span>
          )}
          {run && onRunStatusChange && nextStatus && (
            <button
              type="button"
              className={`btn ${styles.statusButton}`}
              disabled={disabled || statusBusy}
              onClick={() => onRunStatusChange(run, nextStatus)}
            >
              {statusBusy ? 'Saving…' : nextStatus === 'complete' ? 'Mark review complete' : 'Reopen review'}
            </button>
          )}
        </div>
      </div>
    </section>
  );
}
