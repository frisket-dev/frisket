import {
  ChartColumnIncreasing,
  ChevronDown,
  SlidersHorizontal,
} from 'lucide-react';
import {
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from 'react';
import type {
  ReviewBundleOrder,
  ReviewRun,
  ReviewRunCounts,
  ReviewRunField,
  ReviewRunStatus,
} from '../../api/types';
import { useAnchoredPosition } from '../../hooks/useAnchoredPosition';
import { useNativePopover } from '../../hooks/useNativePopover';
import { MenuPop } from '../MenuPop';
import { PanelSelect, type PanelSelectOption } from '../PanelSelect';
import { ReviewResultsPanel } from './ReviewResultsPanel';
import styles from './ReviewRunControls.module.css';

export { ReviewRunSummary } from './ReviewResultsPanel';

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
  /** Row navigation supplied by ReviewQueue, kept beside completion controls. */
  children?: ReactNode;
  /** Paging stays with the queue owner; the toolbar only offers the next page. */
  onLoadMore?(): void;
  hasMore?: boolean;
  loadingMore?: boolean;
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
  children,
  onLoadMore,
  hasMore = false,
  loadingMore = false,
}: ReviewRunControlsProps) {
  const run = runs.find((candidate) => candidate.runId === selectedRunId);
  const counts = run ? selectedCounts(run, selectedFieldId) : null;
  const confidenceAvailable = (counts?.confidenceCount ?? 0) > 0;
  const [orderOpen, setOrderOpen] = useState(false);
  const [resultsOpen, setResultsOpen] = useState(false);
  const orderTriggerRef = useRef<HTMLButtonElement>(null);
  const orderRef = useRef<HTMLDivElement>(null);
  const orderPosition = useAnchoredPosition(orderTriggerRef, {
    enabled: orderOpen,
    width: 250,
    align: 'right',
    gap: 6,
    minHeight: 160,
  });
  useNativePopover(orderRef, () => setOrderOpen(false), {
    enabled: orderOpen,
    ignoreSelector: '[data-review-order-trigger]',
    focusRestore: true,
  });
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
    <section className={styles.toolbar} aria-label="Review scope" data-testid="review-run-controls">
      <PanelSelect
        className={`form-input ${styles.runSelect}`}
        value={selectedRunId ?? ''}
        onValueChange={onSelectedRunChange}
        options={runOptions}
        ariaLabel={run ? `Run: ${reviewProgress(run).label}` : 'Run'}
        disabled={disabled || runOptions.length === 0}
        emptyMessage="No reviewable runs"
        testId="review-run-select"
      />
      {hasMore && onLoadMore && (
        <button
          type="button"
          className={styles.loadMore}
          disabled={disabled || loadingMore}
          onClick={onLoadMore}
        >
          {loadingMore ? 'Loading…' : 'Older runs'}
        </button>
      )}
      <PanelSelect
        className={`form-input ${styles.fieldSelect}`}
        value={selectedFieldId ?? ''}
        onValueChange={(value) => onSelectedFieldChange(value || null)}
        options={fieldOptions}
        ariaLabel="Output field"
        disabled={disabled || !run}
        emptyMessage="No output fields"
        testId="review-field-select"
      />
      {counts && (
        <span className={styles.progress} data-testid="review-run-progress">
          <strong>{counts.reviewedCount}</strong> of {counts.eligibleCount} reviewed
        </span>
      )}
      <button
        type="button"
        className={styles.resultsTrigger}
        aria-haspopup="dialog"
        disabled={disabled || !run}
        onClick={() => setResultsOpen(true)}
      >
        <ChartColumnIncreasing size={15} aria-hidden />
        Results
      </button>
      <button
        ref={orderTriggerRef}
        type="button"
        className={styles.orderTrigger}
        data-review-order-trigger
        aria-label="Review order"
        aria-expanded={orderOpen}
        disabled={disabled || !run}
        onClick={() => setOrderOpen((open) => !open)}
      >
        <SlidersHorizontal size={14} aria-hidden />
        <ChevronDown size={13} aria-hidden />
      </button>
      <span className={styles.spacer} />
      {children && <span className={styles.children}>{children}</span>}
      {run && status === 'complete' && <span className={styles.status}>Complete</span>}
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
      {orderOpen && run && (
        <MenuPop
          ref={orderRef}
          role="dialog"
          aria-label="Review order"
          className={styles.orderMenu}
          data-testid="review-order"
          style={orderPosition ? {
            position: 'fixed', inset: 'auto', margin: 0,
            top: orderPosition.top, bottom: orderPosition.bottom,
            left: orderPosition.left, width: orderPosition.width,
            maxHeight: orderPosition.maxHeight,
          } as CSSProperties : { visibility: 'hidden' }}
        >
          <div className={styles.orderTitle}>Review order</div>
          <div className={styles.orderRow}>
            <span>Order</span>
            <div className={styles.order} role="group" aria-label="Review order">
              <button
                type="button"
                aria-pressed={order === 'shuffle'}
                onClick={() => onOrderChange('shuffle')}
                disabled={disabled}
              >
                Random
              </button>
              <button
                type="button"
                aria-pressed={order === 'confidence'}
                onClick={() => onOrderChange('confidence')}
                disabled={disabled || !confidenceAvailable}
                title={confidenceAvailable ? undefined : 'Lowest confidence is unavailable because this scope has no confidence values.'}
              >
                Lowest confidence
              </button>
              <button
                type="button"
                aria-pressed={order === 'row'}
                onClick={() => onOrderChange('row')}
                disabled={disabled}
              >
                Run order
              </button>
            </div>
          </div>
          {!confidenceAvailable && <p className={styles.noConfidence}>No confidence values in this scope.</p>}
        </MenuPop>
      )}
      {resultsOpen && run && <ReviewResultsPanel run={run} onClose={() => setResultsOpen(false)} />}
      {counts && (
        <div
          className={styles.progressBar}
          role="progressbar"
          aria-label="Review progress"
          aria-valuemin={0}
          aria-valuemax={counts.eligibleCount}
          aria-valuenow={counts.reviewedCount}
          aria-valuetext={`${counts.reviewedCount} of ${counts.eligibleCount} reviewed`}
        >
          <div className={styles.progressFill} style={{ width: `${counts.eligibleCount === 0 ? 0 : Math.min(100, (counts.reviewedCount / counts.eligibleCount) * 100)}%` }} />
        </div>
      )}
    </section>
  );
}
