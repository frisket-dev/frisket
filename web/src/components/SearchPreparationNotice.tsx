import { useState } from 'react';
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronUp, X } from 'lucide-react';
import type { SearchPreparationNotice as Notice } from './useSearchPreparationNotice';

export interface ActivityProgressLine {
  key: string;
  label: string;
  completed: number | null;
  total: number | null;
  detail?: string;
}

export function SearchPreparationNotice({
  notice,
  onReviewPreparation,
  onDismiss,
  onStopPreparation,
  progressLines,
  now,
  stopLabel = 'Stop remaining preparation',
  onResumeImport,
  onKeepImport,
  importActionBusy = false,
  importMessage,
}: {
  notice: Notice;
  onReviewPreparation?: (batchId: string) => void;
  onDismiss?(): void;
  onStopPreparation?(): Promise<void>;
  progressLines?: ActivityProgressLine[];
  now?: string | null;
  stopLabel?: string;
  onResumeImport?(): void;
  onKeepImport?(): void;
  importActionBusy?: boolean;
  importMessage?: string | null;
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

      {!!progressLines?.length && (
        <div className="run-file-progress-list" role="group" aria-label="Import preparation progress">
          {progressLines.map((line) => {
            const boundedTotal = line.total !== null && line.total > 0 ? line.total : null;
            const completed = boundedTotal === null || line.completed === null
              ? null
              : Math.max(0, Math.min(line.completed, boundedTotal));
            return (
              <div className="run-file-progress" key={line.key}>
                <div className="run-file-progress-copy">
                  <span className="run-file-progress-name">{line.label}</span>
                  <span className="run-file-progress-count">
                    {line.detail ?? (completed !== null && boundedTotal !== null
                      ? `${completed.toLocaleString()} / ${boundedTotal.toLocaleString()}`
                      : 'Waiting')}
                  </span>
                </div>
                {boundedTotal !== null && completed !== null ? (
                  <progress
                    className="run-file-progress-bar"
                    aria-label={`${line.label} progress`}
                    max={boundedTotal}
                    value={completed}
                  />
                ) : (
                  <span
                    className="model-pull-progress-bar indeterminate"
                    role="progressbar"
                    aria-label={`${line.label} progress`}
                    aria-valuemin={0}
                    aria-valuemax={100}
                  ><span className="model-pull-progress-fill" /></span>
                )}
              </div>
            );
          })}
        </div>
      )}
      {now && <p className="search-preparation-details" data-testid="import-activity-now">Now · {now} · opened by you</p>}

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
            {currentStop?.busy ? 'Stopping…' : stopLabel}
          </button>
          {currentStop?.error && <span role="alert">{currentStop.error}</span>}
        </div>
      )}
      {(onResumeImport || onKeepImport || importMessage) && (
        <div className="search-preparation-actions">
          {onResumeImport && (
            <button type="button" className="btn btn-compact" disabled={importActionBusy} onClick={onResumeImport}>
              Resume
            </button>
          )}
          {onKeepImport && (
            <button type="button" className="btn btn-compact" disabled={importActionBusy} onClick={onKeepImport}>
              Keep what's added
            </button>
          )}
          {importMessage && <span role="alert">{importMessage}</span>}
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
