import { useState } from 'react';
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronUp, X } from 'lucide-react';
import type { SearchPreparationNotice as Notice } from './useSearchPreparationNotice';

export function SearchPreparationNotice({
  notice,
  onReviewPreparation,
  onDismiss,
  onStopPreparation,
}: {
  notice: Notice;
  onReviewPreparation?: (batchId: string) => void;
  onDismiss?(): void;
  onStopPreparation?(): Promise<void>;
}) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [stopState, setStopState] = useState<{ batchId: string; busy: boolean; error: string | null } | null>(null);
  const currentStop = stopState?.batchId === notice.stopBatchId ? stopState : null;
  const stop = async () => {
    const batchId = notice.stopBatchId;
    if (!batchId || !onStopPreparation || currentStop?.busy) return;
    setStopState({ batchId, busy: true, error: null });
    try {
      await onStopPreparation();
      setStopState((current) => current?.batchId === batchId ? { ...current, busy: false } : current);
    } catch (error) {
      setStopState((current) => current?.batchId === batchId ? {
        batchId, busy: false, error: error instanceof Error ? error.message : String(error),
      } : current);
    }
  };
  const active = notice.tone === 'active';
  const progressLabel = notice.stage === 'download'
    ? notice.detail
    : notice.stage === 'prepare'
      ? 'Preparing files for search'
      : 'Preparing search';

  return (
    <section
      className={`active-run-banner search-preparation-banner status-${notice.tone}`}
      data-testid="search-preparation-banner"
      data-stage={notice.stage}
      role={notice.tone === 'error' ? 'alert' : 'status'}
      aria-live={notice.tone === 'error' ? 'assertive' : 'polite'}
      aria-atomic="true"
    >
      {onDismiss && (
        <button
          type="button"
          className="icon-btn status-notice-dismiss"
          data-testid="search-preparation-dismiss"
          aria-label="Dismiss activity notification"
          onClick={onDismiss}
        >
          <X size={14} aria-hidden />
        </button>
      )}
      <div className="active-run-main">
        {notice.tone === 'error' || notice.tone === 'warning' ? (
          <AlertTriangle size={16} className="run-warning-icon" aria-hidden />
        ) : notice.tone === 'complete' ? (
          <CheckCircle2 size={16} className="search-preparation-complete-icon" aria-hidden />
        ) : (
          <span className="run-spinner" aria-hidden />
        )}
        <div className="active-run-copy">
          <strong>{notice.title}</strong>
          <span>{notice.detail}</span>
        </div>
      </div>

      {((active && (notice.stage === 'download' || notice.stage === 'index' || notice.progress !== null))
        || (notice.tone === 'error' && notice.progress !== null)) && (
        <div className="search-preparation-progress" data-testid="search-preparation-progress">
          <span
            className={`model-pull-progress-bar${notice.progress === null ? ' indeterminate' : ''}`}
            role="progressbar"
            aria-label={progressLabel}
            aria-valuemin={0}
            aria-valuemax={100}
            {...(notice.progress !== null ? { 'aria-valuenow': notice.progress } : {})}
          >
            <span
              className="model-pull-progress-fill"
              style={notice.progress !== null ? { width: `${notice.progress}%` } : undefined}
            />
          </span>
          {notice.progress !== null && <span>{notice.progress}%</span>}
        </div>
      )}

      {!!notice.pdfFiles?.length && (
        <div className="run-file-progress-list" role="group" aria-label="PDF indexing progress">
          {notice.pdfFiles.map((file) => (
            <div className="run-file-progress" key={file.source_id}>
              <div className="run-file-progress-copy">
                <span className="run-file-progress-name">{file.filename || 'PDF document'}</span>
                <span className="run-file-progress-count">
                  {file.completed.toLocaleString()} / {file.total.toLocaleString()} pages · {(file.total - file.completed).toLocaleString()} remaining
                </span>
              </div>
              <progress
                className="run-file-progress-bar"
                aria-label={`Indexing pages for ${file.filename || 'PDF document'}`}
                max={file.total}
                value={file.completed}
              />
            </div>
          ))}
        </div>
      )}
      {notice.pdfFilesTruncated && <p className="search-preparation-details">More PDFs are being indexed.</p>}

      {notice.details.length > 0 && (
        <div className="search-preparation-details">
          <button
            type="button"
            className="search-preparation-details-toggle"
            data-testid="search-preparation-details"
            aria-expanded={detailsOpen}
            onClick={() => setDetailsOpen((open) => !open)}
          >
            {detailsOpen ? <ChevronUp size={12} aria-hidden /> : <ChevronDown size={12} aria-hidden />}
            Details
          </button>
          {detailsOpen && (
            <ul>
              {notice.details.map((detail, index) => <li key={`${index}:${detail}`}>{detail}</li>)}
            </ul>
          )}
        </div>
      )}
      {notice.stopBatchId && onStopPreparation && (
        <div className="search-preparation-actions">
          <button type="button" className="btn btn-compact" disabled={currentStop?.busy}
            onClick={() => void stop()}>
            {currentStop?.busy ? 'Stopping…' : 'Stop remaining preparation'}
          </button>
          {currentStop?.error && <span role="alert">{currentStop.error}</span>}
        </div>
      )}
      {notice.reviewBatchId && onReviewPreparation && (notice.stage === 'approval' || notice.stage === 'failed') && (
        <button
          type="button"
          className="btn btn-compact search-preparation-review"
          data-testid="search-preparation-review"
          onClick={() => onReviewPreparation(notice.reviewBatchId!)}
        >
          {notice.stage === 'approval'
            ? 'Review text'
            : 'Review files'}
        </button>
      )}
    </section>
  );
}
